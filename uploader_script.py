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
# NASA SPEED: 16 parallel workers for massive bandwidth saturation
MAX_WORKERS = 16 
# 1MB chunks are optimal for high-speed parallel MTProto requests
CHUNK_SIZE = 1024 * 1024 

class TurboProgress:
    """Thread-safe high-speed progress tracker."""
    def __init__(self, total):
        self.total = total
        self.current = 0
        self.start_time = time.time()
        self.last_print = 0
        self.lock = asyncio.Lock()

    async def update(self, done_bytes):
        async with self.lock:
            self.current += done_bytes
            now = time.time()
            if now - self.last_print > 0.4 or self.current >= self.total:
                self.last_print = now
                perc = (self.current / self.total) * 100 if self.total > 0 else 0
                elapsed = now - self.start_time
                # MB/s calculation
                speed = (self.current / 1024 / 1024) / elapsed if elapsed > 0 else 0
                sys.stdout.write(
                    f"\r🚀 TURBO: {perc:.1f}% | {self.current/1024/1024:.1f}/{self.total/1024/1024:.1f} MB | {speed:.2f} MB/s \033[K"
                )
                sys.stdout.flush()

async def fast_download(client, message, filename):
    """
    Manually managed multi-part parallel downloader.
    Bypasses standard throttling by distributing load across multiple DC connections.
    """
    if not message or not message.media:
        return None

    file_size = message.file.size
    progress = TurboProgress(file_size)
    
    # Calculate parts
    parts = math.ceil(file_size / CHUNK_SIZE)
    print(f"📡 Initializing Multi-Part Engine | {parts} chunks | {MAX_WORKERS} Workers")

    # File pre-allocation for speed
    with open(filename, 'wb') as f:
        f.truncate(file_size)

    semaphore = asyncio.Semaphore(MAX_WORKERS)
    file_lock = asyncio.Lock()

    async def download_part(part_index):
        async with semaphore:
            offset = part_index * CHUNK_SIZE
            limit = min(CHUNK_SIZE, file_size - offset)
            
            attempts = 0
            while attempts < 5:
                try:
                    # iter_download is the only way to get true raw parallel chunks
                    async for chunk in client.iter_download(
                        message.media, 
                        offset=offset, 
                        limit=limit,
                        request_size=CHUNK_SIZE
                    ):
                        async with file_lock:
                            with open(filename, 'rb+') as f:
                                f.seek(offset)
                                f.write(chunk)
                        await progress.update(len(chunk))
                    return # Success
                except Exception:
                    attempts += 1
                    await asyncio.sleep(1)
            print(f"\n❌ Part {part_index} failed after 5 retries.")

    # Create worker tasks
    tasks = [download_part(i) for i in range(parts)]
    await asyncio.gather(*tasks)
    
    print(f"\n✅ Turbo Download Complete.")
    return filename

def get_simple_metadata(message, filename):
    clean_name = os.path.splitext(filename)[0]
    title = clean_name.replace('_', ' ').replace('.', ' ').strip()
    if len(title) > 95: title = title[:95]
    desc = message.message if message.message else f"Uploaded via Turbo Script: {title}"
    return {"title": title, "description": desc, "tags": ["Turbo", "Telegram"]}

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
        
        print(f"📤 Uploading: {body['snippet']['title']}")
        # 8MB chunks for YT upload saturation
        media = MediaFileUpload(video_path, chunksize=1024*1024*8, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⬆️ YT Progress: {int(status.progress() * 100)}% \033[K", end='\r')
        
        print(f"\n🎉 SUCCESS! https://youtu.be/{response['id']}")
        return True
    except Exception as e:
        print(f"\n🔴 YT Upload Error: {e}")
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
        print(f"\n--- Task: {link} ---")
        chat_id, msg_id = parse_telegram_link(link)
        if not chat_id or not msg_id: return True

        message = await client.get_messages(chat_id, ids=msg_id)
        if not message or not message.media: return True

        fname = message.file.name if hasattr(message.file, 'name') and message.file.name else f"video_{msg_id}.mp4"
        raw_file = f"turbo_{msg_id}_{fname}"
        
        if os.path.exists(raw_file): os.remove(raw_file)

        # Multi-Connection Download
        await fast_download(client, message, raw_file)
        
        # Upload
        metadata = get_simple_metadata(message, fname)
        status = upload_to_youtube(raw_file, metadata)

        if os.path.exists(raw_file): os.remove(raw_file)
        return status
    except Exception as e:
        print(f"🔴 Processing Error: {e}")
        return False

async def run_flow(links_str):
    links = [l.strip() for l in links_str.split(',') if l.strip()]
    try:
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
