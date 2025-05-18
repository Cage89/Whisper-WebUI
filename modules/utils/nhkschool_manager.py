import json
import os
import requests
import urllib.request
from bs4 import BeautifulSoup
from yt_dlp import YoutubeDL

api_url = "https://noa-api.nhk.jp/r1/movies/?dasId={ep_id}&_source=dasId,version,title,noaTitle,noaSubtitle,noaDescription,thumbnailPath,thumbnails,topKeywords,encodings,datePublished,captionPath,partOfSeries,hasParts,isProgram"

# yt_dlp options for extracting audio and converting to mp3
ydl_opts = {
    'format': 'bestaudio/best',
    'outtmpl': 'modules/nhkschool_tmp',  # Output file name
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'quiet': False,  # Set True if you want to suppress output
    'noplaylist': True,
}

def get_nhkschoolaudio(ep_link: str):
    audio_path  = ydl_opts["outtmpl"]+".mp3"
    _, ep_id = ep_link.split("das_id=")
    res = requests.get(api_url.format(ep_id=ep_id))
    soup = BeautifulSoup(res.content, "lxml")
    body = json.loads(soup.body.contents[0].contents[0])
    captions = body["result"][0]["captionPath"]
    if captions is not None:
        audio_path = "https://www.nhk.or.jp" + captions
    else:
        width = body["result"][0]["thumbnails"]["1"]["width"]
        height = body["result"][0]["thumbnails"]["1"]["height"]
        content_path, _ = body["result"][0]["encodings"]["1"]["contentPath"].split(".")
        content_path = "https://vod-stream.nhk.jp" + content_path + f"/index_{width}x{height}_512k.m3u8"
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([content_path])


    title1 = body["result"][0]["noaSubtitle"]
    title2 = body["result"][0]["noaTitle"]
    title = title1 + " " + title2

    return audio_path, title
