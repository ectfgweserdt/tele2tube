import os
import sys
import argparse
import time
import asyncio
import subprocess
import json
import base64
import re
import requests
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import googleapiclient.errors

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025"

# Fetching API Keys
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
OMDB_API_KEY = os.environ.get('OMDB_API_KEY', '').strip() # Added for IMDb info

def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

def download_progress_callback(current, total):
    print(f"⏳ Telegram Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

def clean_search_term(filename):
    """Extracts a clean movie/series title for searching."""
    name = os.path.splitext(filename)[0]
    name = re.sub(r'(_|\.|\-)', ' ', name)
    # Remove technical tags
    tags = [
        r'\d{3,4}p', 'HD', 'NF', 'WEB-DL', 'Dual Audio', 'ES', 'x264', 'x265', 
        'HEVC', 'BluRay', 'HDRip', 'AAC', '5.1', '10bit', r'\[.*?\]', r'\(.*?\)'
    ]
    for tag in tags:
        name = re.sub(tag, '', name, flags=re.IGNORECASE)
    # Remove extra spaces
    return ' '.join(name.split()).strip()

# --- METADATA (OMDb + Gemini) ---
async def get_metadata(filename):
    search_term = clean_search_term(filename)
    print(f"🔍 Searching IMDb (via OMDb) for: {search_term}")
    
    omdb_data = None
    if OMDB_API_KEY:
        try:
            res = requests.get(f"http://www.omdbapi.com/?t={search_term}&apikey={OMDB_API_KEY}", timeout=10)
            data = res.json()
            if data.get("Response") == "True":
                print(f"✅ IMDb Match Found: {data['Title']}")
                omdb_data = data
        except Exception as e:
            print(f"⚠️ OMDb Search failed: {e}")

    # Use Gemini to polish the data or generate it if OMDb failed
    if GEMINI_API_KEY:
        print("🤖 Using Gemini to finalize neat description...")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        
        # Build prompt based on whether we have OMDb data
        if omdb_data:
            prompt = (
                f"I found this movie on IMDb: {json.dumps(omdb_data)}\n"
                "Format this into a professional YouTube metadata set.\n"
                "1. TITLE: Neatly formatted (e.g. 'Movie Name (Year)')\n"
                "2. DESCRIPTION: Include a synopsis, Director, Cast, and a 'Thanks for watching' note.\n"
                "Return as JSON with keys 'title' and 'description'."
            )
        else:
            prompt = (
                f"Analyze filename: '{filename}'. Guess the movie/show.\n"
                "1. TITLE: Clean title (e.g. 'Show Name - S01E01')\n"
                "2. DESCRIPTION: Cinematic overview paragraphs.\n"
                "Return as JSON."
            )

        try:
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"}
            }
            res = requests.post(url, json=payload, timeout=30)
            if res.status_code == 200:
                meta = json.loads(res.json()['candidates'][0]['content']['parts'][0]['text'])
                return meta
        except:
            pass

    # Fallback if both fail
    title = omdb_data['Title'] if omdb_data else search_term
    desc = omdb_data['Plot'] if omdb_data else f"Upload: {search_term}"
    return {"title": title, "description": desc}

# --- FREE THUMBNAIL METHOD ---
def generate_thumbnail_from_video(video_path):
    print("🖼️ Extracting thumbnail from video frame...")
    output_thumb = "thumbnail.jpg"
    try:
        duration_cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 '{video_path}'"
        duration_out, _, _ = run_command(duration_cmd)
        seek_time = float(duration_out.strip()) / 3 if duration_out.strip() else 10
        extract_cmd = f"ffmpeg -ss {seek_time} -i '{video_path}' -vframes 1 -q:v 2 -y {output_thumb}"
        run_command(extract_cmd)
        return output_thumb if os.path.exists(output_thumb) else None
    except: return None

# --- VIDEO PROCESSING ---
def process_video(input_path):
    output_path = "processed_video.mp4"
    print(f"\n🔍 Processing audio tracks...")
    # Map video and keep English audio or first track
    cmd_ffmpeg = (
        f"ffmpeg -i '{input_path}' "
        f"-map 0:v:0 -map 0:a:m:language:eng? -map 0:a:0? "
        f"-c:v copy -c:a aac -b:a 192k -y '{output_path}'"
    )
    _, _, code = run_command(cmd_ffmpeg)
    return output_path if code == 0 and os.path.exists(output_path) else input_path

# --- YOUTUBE UPLOAD ---
def upload_to_youtube(video_path, metadata, thumb_path):
    try:
        creds = Credentials(
            token=None, refresh_token=os.environ.get('YOUTUBE_REFRESH_TOKEN'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.environ.get('YOUTUBE_CLIENT_ID'),
            client_secret=os.environ.get('YOUTUBE_CLIENT_SECRET'),
            scopes=YOUTUBE_SCOPES
        )
        creds.refresh(Request())
        youtube = build('youtube', 'v3', credentials=creds)
        
        body = {
            'snippet': {
                'title': metadata.get('title', 'Video Upload')[:95],
                'description': metadata.get('description', 'High quality content.'),
                'categoryId': '24'
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading: {body['snippet']['title']}")
        media = MediaFileUpload(video_path, chunksize=1024*1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status: print(f"Uploaded {int(status.progress() * 100)}%")

        video_id = response['id']
        if thumb_path:
            time.sleep(5)
            try:
                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
            except: pass
            
        print(f"🎉 DONE: https://youtu.be/{video_id}")
    except Exception as e:
        print(f"🔴 YouTube Error: {e}")

# --- MAIN ---
async def run_flow(link):
    try:
        parts = [p for p in link.strip('/').split('/') if p]
        msg_id = int(parts[-1])
        c_idx = parts.index('c')
        chat_id = int(f"-100{parts[c_idx+1]}")
    except: return

    client = TelegramClient(StringSession(os.environ['TG_SESSION_STRING']), os.environ['TG_API_ID'], os.environ['TG_API_HASH'])
    await client.start()
    message = await client.get_messages(chat_id, ids=msg_id)
    if not message or not message.media: return

    raw_file = f"download_{msg_id}" + (message.file.ext if hasattr(message, 'file') else ".mp4")
    await client.download_media(message, raw_file, progress_callback=download_progress_callback)
    await client.disconnect()

    # Integrated OMDb + AI Metadata
    metadata = await get_metadata(message.file.name or raw_file)
    final_video = process_video(raw_file)
    thumb = generate_thumbnail_from_video(final_video)
    
    upload_to_youtube(final_video, metadata, thumb)

    for f in [raw_file, "processed_video.mp4", "thumbnail.jpg"]:
        if os.path.exists(f): os.remove(f)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
