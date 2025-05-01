import subprocess
import os
import urllib.request
import re
import requests
import json
from bs4 import BeautifulSoup
from multiprocessing.pool import ThreadPool
from datetime import datetime
import shutil
import glob
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import httpx

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

config_url="https://vod.tvp.pl/sess/TVPlayer2/api.php?id={videoId}&@method=getTvpConfig&@callback=callback"
vid_api_url = "https://vod.tvp.pl/api/products/{videoId}/videos/player/configuration?lang=pl&platform=BROWSER&videoType=MOVIE"
new_vid_api_url = "https://vod.tvp.pl/api/products/vods/{videoId}?lang=pl&platform=BROWSER"
widevine_url = "https://vod.tvp.pl/api/products/{videoId}/drm/widevine/external?platform=BROWSER&type=MOVIE"

headers_wv = {
    'accept': '*/*',
    'accept-language': 'en-US,en;q=0.9',
    'origin': 'https://vod.tvp.pl',
    'priority': 'u=1, i',
    'referer': 'https://video.unext.jp/',
    'sec-ch-ua': '"Chromium";v="128", "Not;A=Brand";v="24", "Brave";v="128"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-site',
    'sec-gpc': '1',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'content-type': 'application/octet-stream',
}

headers = {"User-agent": "tvp-tenant5"}

home = Path.home()

def get_keys(license_url, pssh):

    WVD_PATH = next((home / ".wv").glob("*.wvd"))

    device = Device.load(WVD_PATH)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    challenge = cdm.get_license_challenge(session_id, PSSH(pssh))
    response = httpx.post(license_url, data=challenge, headers=headers_wv)

    cdm.parse_license(session_id, response.content)
    keys = []
    for key in cdm.get_keys(session_id):
        if key.type == 'CONTENT':
            keys.append(f"--key {key.kid.hex}:{key.key.hex()}")

    cdm.close(session_id)
    return keys

def try_url(url):
    try: 
        urllib.request.urlopen(url) 
    except urllib.error.HTTPError: 
        error = sys.exc_info()
        return str(error[1])
    return "success"

def binary_search(base_url, q=None, channel="v1"):
    # binary search of number of chunks
    start = 1
    end = 4000

    while True:
        n1 = int((start + end)/2)

        res=try_url(base_url.format(iteration=n1, quality=q, channel=channel))
        if res!="success": # n too large
            end = n1
        else: # n <= val
            start = n1
        print(f"n: {n1} start: {start} end: {end}")
        if (end-start) == 1:
            break

    res=try_url(base_url.format(iteration=end, quality=q, channel=channel))
    return end if res == "success" else start


def fetch_url2(entry):
    path, uri = entry
    if not os.path.exists(path):
        try:
            urllib.request.urlretrieve(uri, path)
        except:
            print("error!")
    return path

def wait_and_push(driver, xpath="", timeout=10):
    timeout=False
    start = datetime.now()
    el = []
    while len(el)<1:
        el = driver.find_elements("xpath", xpath)
        if (datetime.now() - start).seconds>timeout:
            print("timeout waiting for play button!")
            timeout = True
            break
    el[0].click()

def parse_mpd(filepath):
    tree = ET.parse(filepath)
    root = tree.getroot()

    # Define namespaces if present in the XML
    ns = {
        'mpd': 'urn:mpeg:dash:schema:mpd:2011',
        'cenc': 'urn:mpeg:cenc:2013'
    }


    representations_info = []
    pssh_key = None
    for protections in root.findall(".//mpd:ContentProtection", ns):
        pssh = protections.find("cenc:pssh", ns)
        if pssh is not None:
            pssh_key = pssh.text
    # Find all AdaptationSets and their SegmentTemplates
    for adaptation_set in root.findall(".//mpd:AdaptationSet", ns):

        segment_template = adaptation_set.find("mpd:SegmentTemplate", ns)
        if segment_template is None:
            continue

        initialization = segment_template.attrib.get("initialization")
        media = segment_template.attrib.get("media")

        for representation in adaptation_set.findall("mpd:Representation", ns):
            rep_id = representation.attrib.get("id")
            height = representation.attrib.get("height", "audio")  # audio doesn't have height

            rep_info = {
                'id': rep_id,
                'height': height,
                'initialization': initialization.replace('$RepresentationID$', rep_id),
                'media': media.replace('$RepresentationID$', rep_id),
            }
            representations_info.append(rep_info)

    return representations_info, pssh_key

def is_encrypted_mpd(filepath):
    namespaces = {
        'cenc': 'urn:mpeg:cenc:2013',
    }
    root = ET.parse(filepath)

    for elem in root.iter():
        if 'ContentProtection' in elem.tag:
            scheme_id_uri = elem.attrib.get('schemeIdUri', '')
            if 'widevine' in scheme_id_uri.lower() or 'edef8ba9-79d6-4ace-a3c8-27dcd51d21ed' in scheme_id_uri.lower():
                return True
            if 'cenc' in scheme_id_uri.lower() or 'clearkey' in scheme_id_uri.lower():
                return True
            if '{urn:mpeg:cenc:2013}default_KID' in elem.attrib:
                return True
    return False

def get_audio_urls(ep_link, audio_only=False):
    no_manifest = False
    videoId = ep_link.split(",")[-1]
    # try:
    real_api_response = requests.get(new_vid_api_url.format(videoId=videoId), headers=headers)

    real_api_json = json.loads(real_api_response.text)
    real_id = real_api_json['externalUid']

    ep_config = requests.get(config_url.format(videoId=real_id), headers=headers)

    m=re.match("[\s\S]*callback\(([\s\S]*)\)", ep_config.content.decode("utf8"))
    ep_config = json.loads(m.groups()[0])

    qual_manifest = [f for f in ep_config["content"]["files"] if "url" in f and "manifest" in f["url"] or ".mpd" in f["url"]][0]["url"]
    url_root, _ = os.path.split(qual_manifest)
    res = requests.get(qual_manifest, headers=headers)
    # Save to file
    with open("modules/video.mpd", "wb") as f:
        f.write(res.content)

    is_enc = is_encrypted_mpd("modules/video.mpd")


    mpd, pssh_key = parse_mpd("modules/video.mpd")

    if is_enc:
        print("widevine encrypted")
        keys = get_keys(widevine_url.format(videoId=videoId), pssh_key)
        keys = [k.replace("--key", "").strip() for k in keys]

    else:
        print("unencrypted")
        keys = None

    urls = [u for u in mpd if "-a1-" in u["media"]]
    os.remove("modules/video.mpd")
    base_url = url_root + "/" + urls[0]["media"]
    base_init_url = url_root + "/" + urls[0]["initialization"]
    match = re.search(r"-f(\d+)", base_url)
    if match:
        max_qual_a1 = int(match.group(1))
    else:
        max_qual_a1 = None
        print("No -f<number> found in base_init_url.")
    return base_url.replace("$Number$", "{iteration}"), base_init_url, None, max_qual_a1, None, keys

def get_tvptitle(ep_link: str):
    res = requests.get(ep_link)
    soup = BeautifulSoup(res.content, "lxml")
    return soup.find("title").string.replace(" - programy, Oglądaj na TVP VOD", "").replace(" - dla dzieci, Oglądaj na TVP VOD", "").strip()

def convert_mp4_to_wav(input_path, output_path):
    """
    Convert an MP4 file to WAV using ffmpeg via subprocess.

    Parameters:
        input_path (str): Path to the input MP4 file.
        output_path (str): Path to save the output WAV file.
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    command = [
        "ffmpeg",
        "-y",                   # Overwrite output file without asking
        "-i", input_path,       # Input file
        "-ac", "1",             # Convert to mono audio
        "-ar", "16000",         # Sample rate 16kHz (common for ASR models)
        output_path,
    ]

    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"Converted {input_path} to {output_path}")
    except subprocess.CalledProcessError as e:
        print("FFmpeg error:", e.stderr.decode())
        raise

def decrypt_mp4(input_path, keys, output_path="aud_de.mp4"):
    old_cwd = os.getcwd()
    new_cwd, _ = os.path.split(str(Path(input_path).resolve()))
    os.chdir(new_cwd)
    command  =[
        "mp4decrypt",
        "--key",
        *keys,
        input_path,
        output_path,
    ]
    print(" ".join(command))
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"decrypted {input_path} to {output_path}")
    except subprocess.CalledProcessError as e:
        print("mp4decrypt error:", e.stderr.decode())
        raise
    finally:
        os.chdir(old_cwd)

def get_tvpaudio(ep_link: str):
    audio_path  = os.path.join("modules", "tvp_tmp.wav")

    now = datetime.now()
    time_stamp = now.strftime("%Y-%m-%d_%H-%M-%S-%f")
    outfolder_ts = "modules/" + time_stamp
    os.makedirs(outfolder_ts)

    base_url, base_init_url, max_qual, max_qual_a1, n_chunks, keys = get_audio_urls(ep_link)

    quals = {"a1": max_qual_a1}
    if n_chunks is None:
        print("detecting n_chunks")
        n_chunks = binary_search(base_url, max_qual_a1, channel="a1")

    channel = "a1"
    
    print(f"\ndownloading channel {channel}")
    urllib.request.urlretrieve(base_init_url.format(quality=quals[channel], channel=channel), os.path.join(outfolder_ts, f"init_{channel}.mp4"))                             
    print(base_url)
    print(base_init_url)
    print(max_qual)
    urls = [(outfolder_ts + "/part{:04d}_{}_frag.mp4".format(n, channel), base_url.format(iteration=n,quality=quals[channel], channel=channel)) for n in range(1,n_chunks+1)]
    results = ThreadPool(8).imap_unordered(fetch_url2, urls)
    old_prog = 0
    for path in results:
        prog = int(100*int(path.split("/")[-1].replace("part","").replace(channel+"_","").replace("_frag","").replace(".mp4", ""))/n_chunks)
        if prog>old_prog:
            print("{}, ".format(prog), end="")
        old_prog = prog

    print("merging fragments...")
    with open(os.path.join(outfolder_ts,f'{channel}_merge.mp4'), "wb") as f:
        with open(os.path.join(outfolder_ts, f"init_{channel}.mp4"), "rb") as init:
            f.write(init.read())
        for part in glob.glob(f"{outfolder_ts}/*_{channel}_frag.mp4"):
            with open(part, "rb") as p:
                f.write(p.read())

    outfile = f'{outfolder_ts}/{channel}_merge.mp4'

    if keys is not None:
        decrypt_mp4(str(Path(outfile).resolve()), keys, str(Path(f"{outfolder_ts}/aud_de.mp4").resolve()))
        outfile = f"{outfolder_ts}/aud_de.mp4"

    convert_mp4_to_wav(outfile, audio_path)

    shutil.rmtree(outfolder_ts)
    return audio_path
