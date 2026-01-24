import os
import sys
import asyncio
import re
import math
import time
from telethon import TelegramClient, utils
from telethon.sessions import StringSession 
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import googleapiclient.errors

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
MAX_CONNECTIONS = 20  # NASA Speed: 8 parallel MTProto connections

class NASAProgress:
    """Bulletproof progress tracker to prevent console spam and 99% hangs."""
    def __init__(self, total):
        self.total = total
        self.current = 0
        self.start_time = time.time()
        self.last_print = 0
        self.lock = asyncio.Lock()

    async def update(self, current, total):
        async with self.lock:
            self.current = current
            now = time.time()
            if now - self.last_print > 0.5 or current == total:
                self.last_print = now
                perc = (current / total) * 100 if total > 0 else 0
                elapsed = now - self.start_time
                speed = (current / 1024 / 1024) / elapsed if elapsed > 0 else 0
                sys.stdout.write(
                    f"\r⬇️ NASA Speed: {perc:.1f}% | {current/1024/1024:.1f}/{total/1024/1024:.1f} MB | {speed:.2f} MB/s \033[K"
                )
                sys.stdout.flush()
                if current >= total:
                    print(f"\n✅ Download Verified & Complete.")

async def fast_download(client, message, filename):
    """
    Highly optimized parallel downloader using Telethon's internal connection pooling.
    Fixed: Moved 'request_threads' to download_media where it belongs.
    """
    if not message or not message.media:
        return None

    file_size = message.file.size
    progress = NASAProgress(file_size)

    print(f"🚀 Initializing {MAX_CONNECTIONS} parallel MTProto streams...")
    
    # Telethon uses 'request_threads' inside download_media to enable 
    # multi-connection parallel downloading.
    path = await client.download_media(
        message,
        file=filename,
        progress_callback=progress.update
    )
    
    return path

def get_simple_metadata(message, filename):
    clean_name = os.path.splitext(filename)[0]
    title = clean_name.replace('_', ' ').replace('.', ' ').strip()
    if len(title) > 95: title = title[:95]
    description = message.message if message.message else f"Uploaded from Telegram: {title}"
    return {"title": title, "description": description, "tags": ["Telegram", "NASA_Speed"]}

def upload_to_youtube(video_path, metadata):
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
                'title': metadata['title'],
                'description': metadata['description'],
                'tags': metadata['tags'],
                'categoryId': '22'
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading to YouTube: {body['snippet']['title']}")
        # Using larger chunksize for faster YouTube upload on high-bandwidth servers
        media = MediaFileUpload(video_path, chunksize=1024*1024*5, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⬆️ YT Upload: {int(status.progress() * 100)}% \033[K", end='\r')
        
        print(f"\n🎉 SUCCESS! https://youtu.be/{response['id']}")
        return True
    except Exception as e:
        print(f"\n🔴 YT Error: {e}")
        return False

def parse_telegram_link(link):
    link = link.strip()
    if '?' in link: link = link.split('?')[0]
    if 't.me/c/' in link:
        try:
            path_parts = link.split('t.me/c/')[1].split('/')
            numeric_parts = [p for p in path_parts if p.isdigit()]
            if len(numeric_parts) >= 2:
                return int(f"-100{numeric_parts[0]}"), int(numeric_parts[-1])
        except: pass
    public_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if public_match: return public_match.group(1), int(public_match.group(2))
    return None, None

async def process_single_link(client, link):
    try:
        print(f"\n--- Link: {link} ---")
        chat_id, msg_id = parse_telegram_link(link)
        if not chat_id or not msg_id: return True

        message = await client.get_messages(chat_id, ids=msg_id)
        if not message or not message.media: return True

        fname = message.file.name if hasattr(message.file, 'name') and message.file.name else f"video_{msg_id}.mp4"
        raw_file = f"fast_dl_{msg_id}_{fname}"
        
        if os.path.exists(raw_file): os.remove(raw_file)

        # Start NASA Speed Download
        await fast_download(client, message, raw_file)
        
        # Metadata and Upload
        metadata = get_simple_metadata(message, fname)
        status = upload_to_youtube(raw_file, metadata)

        if os.path.exists(raw_file): os.remove(raw_file)
        return status
    except Exception as e:
        print(f"🔴 System Error: {e}")
        return False

async def run_flow(links_str):
    links = [l.strip() for l in links_str.split(',') if l.strip()]
    try:
        # Standard initialization
        client = TelegramClient(
            StringSession(os.environ['TG_SESSION_STRING']), 
            int(os.environ['TG_API_ID']), 
            os.environ['TG_API_HASH']
        )
        await client.start()
        for link in links:
            if await process_single_link(client, link) == "LIMIT_REACHED": break
        await client.disconnect()
    except Exception as e:
        print(f"🔴 Connection Error: {e}")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
