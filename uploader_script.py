import os
import sys
import argparse
import time
import asyncio
import subprocess
import json
import base64
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
IMAGEN_MODEL = "imagen-4.0-generate-001"
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

# --- UTILS ---
def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

def download_progress_callback(current, total):
    print(f"⏳ Telegram Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

# --- AI METADATA ---
async def get_ai_metadata(filename):
    if not GEMINI_API_KEY:
        return {"title": filename, "description": "Auto-upload", "image_prompt": filename}
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    prompt = f"Analyze filename: '{filename}'. 1. Extract formal title (e.g. Series Name S01E01). 2. Write 3-paragraph plot summary/cast using Search. 3. Image prompt for cinematic thumbnail. Return JSON: 'title', 'description', 'image_prompt'."
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    try:
        res = requests.post(url, json=payload)
        res.raise_for_status()
        data = res.json()
        text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Metadata failed: {e}")
        return {"title": filename, "description": "Auto-upload", "image_prompt": filename}

async def generate_thumbnail(image_prompt):
    if not GEMINI_API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGEN_MODEL}:predict?key={GEMINI_API_KEY}"
    payload = {"instances": [{"prompt": f"Cinematic movie poster, no text: {image_prompt}"}], "parameters": {"sampleCount": 1}}
    
    try:
        res = requests.post(url, json=payload)
        data = res.json()
        img_b64 = data.get('predictions', [{}])[0].get('bytesBase64Encoded')
        if img_b64:
            with open("thumbnail.png", "wb") as f:
                f.write(base64.b64decode(img_b64))
            return "thumbnail.png"
    except Exception as e:
        print(f"⚠️ Thumbnail failed: {e}")
    return None

# --- VIDEO PROCESSING ---
def process_video(input_path):
    output_path = "processed_video.mp4"
    print(f"\n🔍 Analyzing stream metadata for {input_path}...")
    
    # Get all audio streams with their language tags
    cmd_probe = f"ffprobe -v error -select_streams a -show_entries stream=index:tags=language -of json '{input_path}'"
    out, _, _ = run_command(cmd_probe)
    
    audio_map = "0:a:0" # Fallback to first audio track
    try:
        data = json.loads(out)
        streams = data.get('streams', [])
        for stream in streams:
            lang = stream.get('tags', {}).get('language', '').lower()
            if 'eng' in lang:
                # We found an English track! 
                # Use the absolute index for mapping
                audio_map = f"0:v:0,0:a:{streams.index(stream)}"
                print(f"✅ Found English audio track at index {stream['index']}")
                break
    except Exception as e:
        print(f"⚠️ Could not parse streams: {e}")

    print("✂️ Filtering video to English audio only...")
    # Map the first video stream and the selected English audio stream
    # -map 0:v:0 -> first video
    # -map 0:a:X -> Xth audio
    # Using 'm:language:eng' is a more robust FFmpeg shortcut
    cmd_ffmpeg = f"ffmpeg -i '{input_path}' -map 0:v:0 -map 0:a:m:language:eng? -map 0:a:0? -disposition:a:0 default -c copy -y '{output_path}'"
    # Explanation:
    # -map 0:a:m:language:eng? -> Try to map English audio. The '?' means don't fail if not found.
    # -map 0:a:0? -> Also map first audio as backup if English isn't explicitly tagged.
    # FFmpeg will pick the best match.
    
    _, err, code = run_command(cmd_ffmpeg)
    
    if code == 0 and os.path.exists(output_path):
        print("✅ FFmpeg processing successful.")
        return output_path
    
    print(f"⚠️ FFmpeg failed (using original file): {err}")
    return input_path

# --- YOUTUBE UPLOAD ---
def upload_to_youtube(video_path, metadata, thumb_path):
    try:
        creds = Credentials(
            token=None,
            refresh_token=os.environ.get('YOUTUBE_REFRESH_TOKEN'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.environ.get('YOUTUBE_CLIENT_ID'),
            client_secret=os.environ.get('YOUTUBE_CLIENT_SECRET'),
            scopes=YOUTUBE_SCOPES
        )
        creds.refresh(Request())
        youtube = build('youtube', 'v3', credentials=creds)
        
        body = {
            'snippet': {
                'title': metadata['title'][:100], 
                'description': metadata['description'] + "\n\n---\nAuto-uploaded from Telegram.", 
                'categoryId': '24'
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading to YouTube: {metadata['title']}")
        media = MediaFileUpload(video_path, chunksize=1024*1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    print(f"Uploaded {int(status.progress() * 100)}%")
            except googleapiclient.errors.HttpError as e:
                if e.resp.status in [500, 502, 503, 504]:
                    time.sleep(5)
                    continue
                else: raise

        video_id = response['id']
        if thumb_path:
            print("🖼️ Applying AI-generated thumbnail...")
            try:
                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
            except Exception as te:
                print(f"⚠️ Thumbnail upload failed: {te}")
                
        print(f"✅ Success! Video Link: https://youtu.be/{video_id}")
        
    except googleapiclient.errors.ResumableUploadError as e:
        if "uploadLimitExceeded" in str(e):
            print("\n❌ FATAL ERROR: YouTube Daily Upload Limit Exceeded.")
            print("YouTube limits new accounts to a certain number of uploads per day.")
            print("Please wait 24 hours before trying again.")
        else:
            print(f"🔴 YouTube Upload Error: {e}")
    except Exception as e:
        print(f"🔴 General Upload Error: {e}")

# --- MAIN ---
async def run_flow(link):
    # Parse Link
    try:
        parts = [p for p in link.strip('/').split('/') if p]
        msg_id = int(parts[-1])
        # Find 'c' and get the next part for chat_id
        c_idx = parts.index('c')
        chat_id = int(f"-100{parts[c_idx+1]}")
    except Exception as e:
        print(f"🔴 Link parsing failed: {e}")
        return

    client = TelegramClient(StringSession(os.environ['TG_SESSION_STRING']), os.environ['TG_API_ID'], os.environ['TG_API_HASH'])
    
    try:
        await client.start()
        print(f"📡 Connected. Fetching message {msg_id}...")
        message = await client.get_messages(chat_id, ids=msg_id)
        
        if not message or not message.media:
            print("🔴 No media found in that message.")
            return

        # Use original extension from Telegram
        ext = message.file.ext if hasattr(message, 'file') and message.file.ext else ".mp4"
        raw_file = f"downloaded_{msg_id}{ext}"
        
        print(f"⬇️ Downloading '{message.file.name or raw_file}'...")
        await client.download_media(message, raw_file, progress_callback=download_progress_callback)
        print("\n✅ Download finished.")
        await client.disconnect()

        # AI & Processing
        metadata = await get_ai_metadata(message.file.name or raw_file)
        thumb = await generate_thumbnail(metadata['image_prompt'])
        final_video = process_video(raw_file)

        # Upload
        upload_to_youtube(final_video, metadata, thumb)
        
    except Exception as e:
        print(f"🔴 Flow Error: {e}")
    finally:
        # Cleanup
        for f in [raw_file, "processed_video.mp4", "thumbnail.png"]:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
    else:
        print("Usage: python uploader_script.py <telegram_link>")
