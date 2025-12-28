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
YOUTUBE_SCOPES = ['[https://www.googleapis.com/auth/youtube.upload](https://www.googleapis.com/auth/youtube.upload)']
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025"
IMAGEN_MODEL = "imagen-4.0-generate-001"
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

def download_progress_callback(current, total):
    print(f"⏳ Telegram Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

# --- AI METADATA ---
async def get_ai_metadata(filename):
    print(f"🤖 Calling Gemini AI for metadata: {filename}")
    
    # Pre-clean filename for the prompt
    clean_name = filename.replace('_', ' ').replace('.', ' ')
    
    if not GEMINI_API_KEY:
        print("⚠️ No GEMINI_API_KEY found in secrets!")
        return {"title": clean_name, "description": "High-quality series upload.", "image_prompt": clean_name}
    
    url = f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/){GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    
    prompt = (
        f"You are a YouTube SEO expert. Analyze this filename: '{filename}'\n"
        "REQUIRED ACTIONS:\n"
        "1. CLEAN TITLE: Search for the real movie/series name. Return a beautiful title like 'Love, Death & Robots - Season 4 Episode 9'. Remove all technical tags like 720p, WEB-DL, Dual Audio, etc.\n"
        "2. DESCRIPTION: Write a professional 3-paragraph summary. Include cast info and plot without spoilers. DO NOT include any external links or 'Auto-uploaded' text.\n"
        "3. IMAGE PROMPT: Create a vivid, high-detail description for an AI image generator to create a cinematic poster for this specific content.\n"
        "\nIMPORTANT: Your response must be valid JSON only."
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    try:
        res = requests.post(url, json=payload, timeout=45)
        res.raise_for_status()
        data = res.json()
        
        # Extract text content carefully
        raw_text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
        
        # Clean up possible markdown wrapping from the response
        json_str = re.sub(r'```json\n?|\n?```', '', raw_text).strip()
        meta = json.loads(json_str)
        
        if 'title' in meta:
            print(f"✅ AI metadata generated: {meta['title']}")
            return meta
    except Exception as e:
        print(f"⚠️ AI Metadata failed: {e}. Using basic cleanup.")
    
    # Robust fallback title cleanup
    fallback_title = re.sub(r'(_|\.mkv|\.mp4|\.avi|\.720p|\.1080p|HD|WEB-DL|Dual Audio)', ' ', filename).strip()
    return {
        "title": fallback_title, 
        "description": f"Detailed video for {fallback_title}. High quality upload.", 
        "image_prompt": fallback_title
    }

async def generate_thumbnail(image_prompt):
    if not GEMINI_API_KEY: return None
    print(f"🎨 Generating AI Thumbnail...")
    
    url = f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/){IMAGEN_MODEL}:predict?key={GEMINI_API_KEY}"
    payload = {
        "instances": [{"prompt": f"Cinematic movie poster, digital art, high detail, masterpiece, no text: {image_prompt}"}],
        "parameters": {"sampleCount": 1}
    }
    
    try:
        res = requests.post(url, json=payload, timeout=60)
        res.raise_for_status()
        data = res.json()
        img_b64 = data.get('predictions', [{}])[0].get('bytesBase64Encoded')
        if img_b64:
            path = "thumbnail.png"
            with open(path, "wb") as f:
                f.write(base64.b64decode(img_b64))
            print("✅ Thumbnail file created.")
            return path
    except Exception as e:
        print(f"⚠️ Thumbnail generation failed: {e}")
    return None

# --- VIDEO PROCESSING ---
def process_video(input_path):
    output_path = "processed_video.mp4"
    print(f"\n🔍 Optimizing video for YouTube...")
    
    # FFmpeg: Pick English audio, convert to AAC if needed, copy video stream (fast)
    cmd_ffmpeg = (
        f"ffmpeg -i '{input_path}' "
        f"-map 0:v:0 -map 0:a:m:language:eng? -map 0:a:0? "
        f"-c:v copy -c:a aac -b:a 192k -y '{output_path}'"
    )
    
    _, err, code = run_command(cmd_ffmpeg)
    if code == 0 and os.path.exists(output_path):
        return output_path
    return input_path

# --- YOUTUBE UPLOAD ---
def upload_to_youtube(video_path, metadata, thumb_path):
    try:
        creds = Credentials(
            token=None,
            refresh_token=os.environ.get('YOUTUBE_REFRESH_TOKEN'),
            token_uri='[https://oauth2.googleapis.com/token](https://oauth2.googleapis.com/token)',
            client_id=os.environ.get('YOUTUBE_CLIENT_ID'),
            client_secret=os.environ.get('YOUTUBE_CLIENT_SECRET'),
            scopes=YOUTUBE_SCOPES
        )
        creds.refresh(Request())
        youtube = build('youtube', 'v3', credentials=creds)
        
        final_title = metadata.get('title', 'Video Upload')[:95]
        
        body = {
            'snippet': {
                'title': final_title,
                'description': metadata.get('description', 'High quality content.'),
                'categoryId': '24' # Entertainment
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading to YouTube: {final_title}")
        media = MediaFileUpload(video_path, chunksize=1024*1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status: print(f"Uploaded {int(status.progress() * 100)}%")

        video_id = response['id']
        
        if thumb_path and os.path.exists(thumb_path):
            print(f"🖼️ Setting thumbnail for {video_id}...")
            # Wait 5 seconds for YouTube to process metadata before sending thumbnail
            time.sleep(5)
            try:
                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
                print("✅ Thumbnail applied successfully!")
            except Exception as te:
                print(f"⚠️ Thumbnail apply error: {te}")
            
        print(f"🎉 SUCCESS! [https://youtu.be/](https://youtu.be/){video_id}")
        
    except Exception as e:
        print(f"🔴 YouTube Error: {e}")

# --- MAIN ---
async def run_flow(link):
    try:
        parts = [p for p in link.strip('/').split('/') if p]
        msg_id = int(parts[-1])
        c_idx = parts.index('c')
        chat_id = int(f"-100{parts[c_idx+1]}")
    except:
        print("Invalid Telegram link.")
        return

    client = TelegramClient(StringSession(os.environ['TG_SESSION_STRING']), os.environ['TG_API_ID'], os.environ['TG_API_HASH'])
    
    await client.start()
    message = await client.get_messages(chat_id, ids=msg_id)
    if not message or not message.media: return

    ext = ".mp4"
    if hasattr(message, 'file') and message.file.ext: ext = message.file.ext
    raw_file = f"downloaded_{msg_id}{ext}"
    
    print(f"⬇️ Downloading: {message.file.name or raw_file}")
    await client.download_media(message, raw_file, progress_callback=download_progress_callback)
    await client.disconnect()

    # Generate metadata and thumbnail concurrently
    metadata = await get_ai_metadata(message.file.name or raw_file)
    thumb_task = generate_thumbnail(metadata['image_prompt'])
    
    final_video = process_video(raw_file)
    thumb = await thumb_task

    upload_to_youtube(final_video, metadata, thumb)

    # Cleanup local files
    for f in [raw_file, "processed_video.mp4", "thumbnail.png"]:
        if os.path.exists(f): os.remove(f)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
