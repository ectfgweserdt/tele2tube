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
PARALLEL_CHUNKS = 4  # Number of parallel downloads.

# Global to track last print time to prevent spam
last_print_time = 0

def download_progress_callback(current, total):
    global last_print_time
    now = time.time()
    
    # Only print if 0.5 seconds passed OR it's the final byte (100%)
    if total and (now - last_print_time > 0.5 or current == total):
        last_print_time = now
        percentage = min(100.0, current * 100 / total)
        print(f"⬇️ Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({percentage:.1f}%)", end='\r')

async def fast_download(client, message, filename, progress_callback=None):
    """
    Downloads a file in parallel chunks with NASA-tier speed and no console spam.
    """
    msg_media = message.media
    if not msg_media:
        return None
        
    document = msg_media.document if hasattr(msg_media, 'document') else msg_media
    file_size = document.size
    
    part_size = 10 * 1024 * 1024 # 10MB chunks
    part_count = math.ceil(file_size / part_size)
    
    print(f"🚀 Starting Fast Download ({PARALLEL_CHUNKS} threads) for {file_size/1024/1024:.2f} MB...")

    file_lock = asyncio.Lock()
    progress_lock = asyncio.Lock()
    downloaded_bytes = 0
    
    with open(filename, 'wb') as f:
        f.truncate(file_size)
        
        queue = asyncio.Queue()
        for i in range(part_count):
            queue.put_nowait(i)
            
        async def worker():
            nonlocal downloaded_bytes
            while not queue.empty():
                try:
                    part_index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                
                offset = part_index * part_size
                current_limit = min(part_size, file_size - offset)
                
                try:
                    current_file_pos = offset
                    async for chunk in client.iter_download(
                        message.media, 
                        offset=offset, 
                        limit=current_limit,
                        request_size=512*1024 
                    ):
                        chunk_len = len(chunk)
                        
                        async with file_lock:
                            f.seek(current_file_pos)
                            f.write(chunk)
                        
                        current_file_pos += chunk_len
                        
                        async with progress_lock:
                            downloaded_bytes += chunk_len
                            if progress_callback:
                                progress_callback(min(downloaded_bytes, file_size), file_size)
                            
                except Exception as e:
                    print(f"⚠️ Chunk {part_index} failed, retrying...")
                    queue.put_nowait(part_index) 
                finally:
                    queue.task_done()

        tasks = [asyncio.create_task(worker()) for _ in range(PARALLEL_CHUNKS)]
        await asyncio.gather(*tasks)

    print(f"\n✅ Fast Download Complete: {filename}")
    return filename

def get_simple_metadata(message, filename):
    clean_name = os.path.splitext(filename)[0]
    title = clean_name.replace('_', ' ').replace('.', ' ').strip()
    if len(title) > 95:
        title = title[:95]
    description = message.message if message.message else f"Uploaded from Telegram: {title}"
    tags = ["Telegram", "Video", "Upload"]
    return {"title": title, "description": description, "tags": tags}

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
        
        print(f"🚀 Uploading to YT: {body['snippet']['title']}")
        media = MediaFileUpload(video_path, chunksize=1024*1024*2, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⬆️ Upload: {int(status.progress() * 100)}%", end='\r')
        
        print(f"\n🎉 SUCCESS! https://youtu.be/{response['id']}")
        return True

    except googleapiclient.errors.HttpError as e:
        error_details = e.content.decode()
        if "uploadLimitExceeded" in error_details or "quotaExceeded" in error_details:
            print("\n❌ YOUTUBE QUOTA EXCEEDED!")
            return "LIMIT_REACHED"
        print(f"\n🔴 YT HTTP Error: {e}")
        return False
    except Exception as e:
        print(f"\n🔴 Upload Error: {e}")
        return False

def parse_telegram_link(link):
    link = link.strip()
    if '?' in link:
        link = link.split('?')[0]
    if 't.me/c/' in link:
        try:
            path_parts = link.split('t.me/c/')[1].split('/')
            numeric_parts = [p for p in path_parts if p.isdigit()]
            if len(numeric_parts) >= 2:
                chat_id = int(f"-100{numeric_parts[0]}")
                msg_id = int(numeric_parts[-1])
                return chat_id, msg_id
        except: pass
    public_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if public_match:
        return public_match.group(1), int(public_match.group(2))
    return None, None

async def process_single_link(client, link):
    try:
        print(f"\n--- Processing: {link} ---")
        chat_id, msg_id = parse_telegram_link(link)
        if not chat_id or not msg_id:
            print(f"❌ Invalid Link: {link}")
            return True

        message = await client.get_messages(chat_id, ids=msg_id)
        if not message or not message.media:
            print("❌ No media found.")
            return True

        original_filename = message.file.name if hasattr(message.file, 'name') and message.file.name else f"video_{msg_id}.mp4"
        raw_file = f"dl_{msg_id}_{original_filename}"
        
        if os.path.exists(raw_file): os.remove(raw_file)

        await fast_download(client, message, raw_file, progress_callback=download_progress_callback)
        metadata = get_simple_metadata(message, original_filename)
        status = upload_to_youtube(raw_file, metadata)

        if os.path.exists(raw_file): os.remove(raw_file)
        return status
    except Exception as e:
        print(f"🔴 Error: {e}")
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
        print(f"🔴 Client Error: {e}")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
