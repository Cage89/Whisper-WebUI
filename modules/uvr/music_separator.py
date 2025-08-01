from typing import Optional, Union, List, Dict
import numpy as np
import torchaudio
import soundfile as sf
import os
import torch
import gc
import gradio as gr
from datetime import datetime
import traceback

from modules.utils.paths import DEFAULT_PARAMETERS_CONFIG_PATH, UVR_MODELS_DIR, UVR_OUTPUT_DIR, MELBANDUVR_MODELS_DIR
from modules.utils.files_manager import load_yaml, save_yaml, is_video
from modules.diarize.audio_loader import load_audio
from modules.utils.logger import get_logger
logger = get_logger()

try:
    from uvr.models import MDX, Demucs, VrNetwork, MDXC
except Exception as e:
    logger.warning(
        "Failed to import uvr. BGM separation feature will not work. "
        "Please open an issue on GitHub if you encounter this error. "
        f"Error: {type(e).__name__}: {traceback.format_exc()}"
    )

from audio_separator.separator import Separator as MelbandUVRSeparator

melband_models = {
    "MelBand Roformer Kim | Big Beta v5e": "melband_roformer_big_beta5e.ckpt",
    "MelBand Roformer | Big Beta 6 by unwa": "melband_roformer_big_beta6.ckpt",
    "MelBand Roformer | Big Beta 6X by unwa": "melband_roformer_big_beta6x.ckpt",
}

class MusicSeparator:
    def __init__(self,
                 model_dir: Optional[str] = UVR_MODELS_DIR,
                 output_dir: Optional[str] = UVR_OUTPUT_DIR):
        self.model = None
        self.melbanduvr_separator = None
        self.device = self.get_device()
        self.available_devices = ["cpu", "cuda", "xpu", "mps"]
        self.model_dir = model_dir
        self.output_dir = output_dir
        instrumental_output_dir = os.path.join(self.output_dir, "instrumental")
        vocals_output_dir = os.path.join(self.output_dir, "vocals")
        os.makedirs(instrumental_output_dir, exist_ok=True)
        os.makedirs(vocals_output_dir, exist_ok=True)
        self.audio_info = None
        self.available_models = [
            "UVR-MDX-NET-Inst_HQ_4",
            "UVR-MDX-NET-Inst_3",
            "MelBand Roformer Kim | Big Beta v5e",
            "MelBand Roformer | Big Beta 6 by unwa",
            "MelBand Roformer | Big Beta 6X by unwa"
        ]
        self.default_model = self.available_models[0]
        self.current_model_size = self.default_model
        self.model_config = {
            "segment": 256,
            "split": True
        }

    def is_melbanduvr_model(self, model_name: str) -> bool:
        return "Roformer" in model_name or "Big Beta" in model_name

    def update_model(self,
                     model_name: str = "UVR-MDX-NET-Inst_1",
                     device: Optional[str] = None,
                     segment_size: int = 256):
        if device is None:
            device = self.device

        self.device = device
        self.model_config = {
            "segment": segment_size,
            "split": True
        }

        self.current_model_size = model_name

        if self.is_melbanduvr_model(model_name):
            if self.melbanduvr_separator is None:
                self.melbanduvr_separator = MelbandUVRSeparator(model_file_dir=MELBANDUVR_MODELS_DIR, sample_rate=16e3)
            self.melbanduvr_separator.load_model(melband_models[model_name])
            self.model = self.melbanduvr_separator
        else:
            self.model = MDX(name=model_name,
                             other_metadata=self.model_config,
                             device=self.device,
                             logger=None,
                             model_dir=self.model_dir)

    def separate(self,
                 audio: Union[str, np.ndarray],
                 model_name: str,
                 device: Optional[str] = None,
                 segment_size: int = 256,
                 save_file: bool = False,
                 progress: gr.Progress = gr.Progress()) -> tuple[np.ndarray, np.ndarray, List]:
        if isinstance(audio, str):
            output_filename, ext = os.path.basename(audio), ".wav"
            output_filename, orig_ext = os.path.splitext(output_filename)

            if is_video(audio):
                audio = load_audio(audio)
                sample_rate = 16000
            else:
                self.audio_info = torchaudio.info(audio)
                sample_rate = self.audio_info.sample_rate
        else:
            timestamp = datetime.now().strftime("%m%d%H%M%S")
            output_filename, ext = f"UVR-{timestamp}", ".wav"
            sample_rate = 16000

        model_config = {
            "segment": segment_size,
            "split": True
        }

        if (self.model is None or
                self.current_model_size != model_name or
                self.model_config != model_config or
                (not self.is_melbanduvr_model(model_name) and getattr(self.model, "sample_rate", sample_rate) != sample_rate) or
                self.device != device):
            progress(0, desc="Initializing Separation Model..")
            self.update_model(
                model_name=model_name,
                device=device,
                segment_size=segment_size
            )
            if not self.is_melbanduvr_model(model_name):
                self.model.sample_rate = sample_rate

        progress(0, desc="Separating background music from the audio..")
        if self.is_melbanduvr_model(model_name):
            if isinstance(audio, np.ndarray):
                # Save temp wav file for MelbandUVR
                temp_path = os.path.join(self.output_dir, f"temp_input_{datetime.now().strftime('%H%M%S')}.wav")
                sf.write(temp_path, audio, sample_rate, format="WAV")
                audio = temp_path

            result_paths = self.model.separate(audio)
            logger.info(result_paths)
            logger.info(p for p in result_paths)
            logger.info(f"result_paths: {len(result_paths)}")
            vocals_path = [p for p in result_paths if "(vocals)" in p.lower()][0]
            instrumental_path = [p for p in result_paths if "(other)" in p.lower()][0]
            vocals, sr = sf.read(vocals_path)
            instrumental, sr = sf.read(instrumental_path)

            file_paths = []

            if save_file:
                instrumental_output_path = os.path.join(self.output_dir, "instrumental", f"{output_filename}-instrumental{ext}")
                vocals_output_path = os.path.join(self.output_dir, "vocals", f"{output_filename}-vocals{ext}")
                sf.write(instrumental_output_path, instrumental, sr, format="WAV")
                sf.write(vocals_output_path, vocals, sr, format="WAV")
                file_paths = [instrumental_output_path, vocals_output_path]
            vocals = vocals.astype(np.float32)
            instrumental = instrumental.astype(np.float32)

        else:
            result = self.model(audio)
            instrumental, vocals = result["instrumental"].T, result["vocals"].T
            file_paths = []

            if save_file:
                instrumental_output_path = os.path.join(self.output_dir, "instrumental", f"{output_filename}-instrumental{ext}")
                vocals_output_path = os.path.join(self.output_dir, "vocals", f"{output_filename}-vocals{ext}")
                sf.write(instrumental_output_path, instrumental, sample_rate, format="WAV")
                sf.write(vocals_output_path, vocals, sample_rate, format="WAV")
                file_paths = [instrumental_output_path, vocals_output_path]

        return instrumental, vocals, file_paths

    def separate_files(self,
                       files: List,
                       model_name: str,
                       device: Optional[str] = None,
                       segment_size: int = 256,
                       save_file: bool = True,
                       progress: gr.Progress = gr.Progress()) -> List[str]:
        """Separate the background music from the audio files. Returns only last Instrumental and vocals file paths
        to display into gr.Audio()"""
        self.cache_parameters(model_size=model_name, segment_size=segment_size)

        for file_path in files:
            instrumental, vocals, file_paths = self.separate(
                audio=file_path,
                model_name=model_name,
                device=device,
                segment_size=segment_size,
                save_file=save_file,
                progress=progress
            )
        return file_paths

    @staticmethod
    def get_device():
        if torch.cuda.is_available():
            return "cuda"
        if torch.xpu.is_available():
            return "xpu"
        elif torch.backends.mps.is_available():
            return "mps"
        else:
            return "cpu"

    def offload(self):
        """Offload the model and free up the memory"""
        if self.model is not None:
            del self.model
            self.model = None
        if self.device == "cuda":
            torch.cuda.empty_cache()
            torch.cuda.reset_max_memory_allocated()
        if self.device == "xpu":
            torch.xpu.empty_cache()
            torch.xpu.reset_accumulated_memory_stats()
            torch.xpu.reset_peak_memory_stats()
        gc.collect()
        self.audio_info = None

    @staticmethod
    def cache_parameters(model_size: str,
                         segment_size: int):
        cached_params = load_yaml(DEFAULT_PARAMETERS_CONFIG_PATH)
        cached_uvr_params = cached_params["bgm_separation"]
        uvr_params_to_cache = {
            "model_size": model_size,
            "segment_size": segment_size
        }
        cached_uvr_params = {**cached_uvr_params, **uvr_params_to_cache}
        cached_params["bgm_separation"] = cached_uvr_params
        save_yaml(cached_params, DEFAULT_PARAMETERS_CONFIG_PATH)