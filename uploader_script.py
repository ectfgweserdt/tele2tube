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
OMDB_API_KEY = os.environ.get('OMDB_API_KEY', '').strip() 

def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

def download_progress_callback(current, total):
    print(f"⏳ Telegram Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

def parse_filename(filename):
    """Extracts title, season, and episode for better IMDb searching."""
    clean_name = os.path.splitext(filename)[0].replace('_', ' ').replace('.', ' ')
    
    # Try to find S01E01 patterns
    match = re.search(r'S(\d+)E(\d+)', clean_name, re.IGNORECASE)
    season, episode = None, None
    if match:
        season = match.group(1)
        episode = match.group(2)
        # Remove SxxExx and everything after from the search term
        search_title = clean_name[:match.start()].strip()
    else:
        # Fallback cleaning for movies
        tags = [r'\d{3,4}p', 'HD', 'NF', 'WEB-DL', 'Dual Audio', 'ES', 'x264', 'x265', 'HEVC']
        search_title = clean_name
        for tag in tags:
            search_title = re.sub(tag, '', search_title, flags=re.IGNORECASE)
        search_title = ' '.join(search_title.split()).strip()
        
    return search_title, season, episode

# --- METADATA ENGINE ---
async def get_metadata(filename):
    search_title, season, episode = parse_filename(filename)
    print(f"🔍 Searching IMDb for: {search_title} " + (f"(S{season}E{episode})" if season else ""))
    
    omdb_data = None
    if OMDB_API_KEY:
        try:
            url = f"http://www.omdbapi.com/?t={search_title}&apikey={OMDB_API_KEY}"
            if season: url += f"&Season={season}&Episode={episode}"
            
            res = requests.get(url, timeout=10)
            data = res.json()
            if data.get("Response") == "True":
                print(f"✅ IMDb Match: {data['Title']}")
                omdb_data = data
        except Exception as e:
            print(f"⚠️ IMDb Search failed: {e}")

    # Use Gemini to polish the metadata into a "Neat" format
    if GEMINI_API_KEY:
        print("🤖 AI is formatting neat description...")
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        
        prompt = (
            f"Context: Filename is '{filename}'. IMDb Data: {json.dumps(omdb_data) if omdb_data else 'None found'}.\n\n"
            "Task: Create NEAT YouTube metadata. Follow these rules exactly:\n"
            "1. TITLE: Clean and formal. Example: 'Movie Name (Year)' or 'Series Name - S01E05 - Episode Name'. No technical tags.\n"
            "2. DESCRIPTION: Must include these sections with emojis:\n"
            "   🎬 SYNOPSIS: (3-4 sentences summarizing the plot)\n"
            "   🎭 CAST & CREW: (List main actors and director)\n"
            "   📌 DETAILS: (Release Year, Genre, IMDb Rating)\n"
            "   🚀 Follow for more high-quality content!\n"
            "3. TAGS: Provide 10 relevant SEO keywords separated by commas.\n\n"
            "Return ONLY a JSON object with keys: 'title', 'description', 'tags'."
        )

        try:
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"}
            }
            res = requests.post(gemini_url, json=payload, timeout=30)
            if res.status_code == 200:
                meta = json.loads(res.json()['candidates'][0]['content']['parts'][0]['text'])
                return meta
        except Exception as e:
            print(f"⚠️ AI Formatting failed: {e}")

    # Final Fallback
    return {
        "title": omdb_data['Title'] if omdb_data else search_title,
        "description": f"Plot: {omdb_data.get('Plot', 'No description available.')}\n\nUpload of {filename}",
        "tags": "movies, series, entertainment"
    }

# --- FREE THUMBNAIL METHOD ---
def generate_thumbnail_from_video(video_path):
    print("🖼️ Extracting high-quality frame for thumbnail...")
    output_thumb = "thumbnail.jpg"
    try:
        duration_cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 '{video_path}'"
        duration_out, _, _ = run_command(duration_cmd)
        # Take frame at 25% to avoid spoilers but miss intros
        seek_time = float(duration_out.strip()) / 4 if duration_out.strip() else 15
        extract_cmd = f"ffmpeg -ss {seek_time} -i '{video_path}' -vframes 1 -q:v 2 -y {output_thumb}"
        run_command(extract_cmd)
        return output_thumb if os.path.exists(output_thumb) else None
    except: return None

# --- VIDEO PROCESSING ---
def process_video(input_path):
    output_path = "processed_video.mp4"
    print(f"\n🔍 Optimizing video & audio...")
    # Keep video, keep English audio (or first track), convert to standard AAC
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
                'tags': metadata.get('tags', '').split(','),
                'categoryId': '24' # Entertainment
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading to YouTube: {body['snippet']['title']}")
        media = MediaFileUpload(video_path, chunksize=1024*1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status: print(f"Uploaded {int(status.progress() * 100)}%")

        video_id = response['id']
        
        if thumb_path:
            print("🖼️ Applying thumbnail...")
            time.sleep(10) # Wait for YouTube backend
            try:
                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
                print("✅ Thumbnail set!")
            except: pass
            
        print(f"🎉 SUCCESS! Link: https://youtu.be/{video_id}")
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
    print(f"⬇️ Downloading from Telegram...")
    await client.download_media(message, raw_file, progress_callback=download_progress_callback)
    await client.disconnect()

    # Get Metadata (IMDb + AI formatting)
    metadata = await get_metadata(message.file.name or raw_file)
    
    # Video & Thumb
    final_video = process_video(raw_file)
    thumb = generate_thumbnail_from_video(final_video)
    
    # Upload
    upload_to_youtube(final_video, metadata, thumb)

    # Cleanup
    for f in [raw_file, "processed_video.mp4", "thumbnail.jpg"]:
        if os.path.exists(f): os.remove(f)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
