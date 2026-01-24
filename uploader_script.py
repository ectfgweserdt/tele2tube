import os
import sys
import time
import asyncio
import hashlib
import math
from telethon import TelegramClient, errors, utils
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# --- CONFIGURATION ---
PARALLEL_CONNECTIONS = 16  # High-speed sweet spot
CHUNK_SIZE = 1024 * 1024   # 1MB chunks

class FastDownloader:
    """Bypasses Telegram speed limits using parallel connections."""
    def __init__(self, client, message, file_path):
        self.client = client
        self.message = message
        self.file_path = file_path
        self.total_size = message.file.size
        self.downloaded = 0

    async def download_part(self, offset, limit, part_index):
        async for chunk in self.client.iter_download(self.message.document, offset=offset, limit=limit):
            with open(f"{self.file_path}.part{part_index}", "ab") as f:
                f.write(chunk)
                self.downloaded += len(chunk)
                percent = (self.downloaded / self.total_size) * 100
                print(f"\r🚀 Downloading: {percent:.2f}%", end="")

    async def download(self):
        part_size = math.ceil(self.total_size / PARALLEL_CONNECTIONS)
        tasks = []
        for i in range(PARALLEL_CONNECTIONS):
            offset = i * part_size
            # Ensure the last part doesn't exceed total size
            limit = min(part_size, self.total_size - offset)
            if limit <= 0: break
            tasks.append(self.download_part(offset, limit, i))
        
        await asyncio.gather(*tasks)
        
        # Combine parts
        with open(self.file_path, "wb") as final_file:
            for i in range(PARALLEL_CONNECTIONS):
                part_name = f"{self.file_path}.part{i}"
                if os.path.exists(part_name):
                    with open(part_name, "rb") as pf:
                        final_file.write(pf.read())
                    os.remove(part_name)
        print(f"\n✅ Download Complete: {self.file_path}")

def get_lecture_title(filename):
    """Cleans file name for a professional YouTube title."""
    name = os.path.splitext(filename)[0]
    # Remove common clutter like underscores or dots
    name = name.replace("_", " ").replace(".", " ")
    return name.title()

def upload_to_youtube(file_path, title):
    """Simple upload logic for lectures."""
    creds = Credentials(
        None,
        refresh_token=os.environ['YOUTUBE_REFRESH_TOKEN'],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ['YOUTUBE_CLIENT_ID'],
        client_secret=os.environ['YOUTUBE_CLIENT_SECRET']
    )
    
    youtube = build("youtube", "v3", credentials=creds)
    
    body = {
        "snippet": {
            "title": title[:100],
            "description": f"Uploaded lecture: {title}\nAutomated Archive.",
            "categoryId": "27" # Education
        },
        "status": {"privacyStatus": "private"}
    }
    
    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    
    print(f"📤 Uploading: {title}...")
    response = request.execute()
    print(f"🎉 SUCCESS: https://youtu.be/{response['id']}")

async def process_link(client, link):
    try:
        # Extract IDs from link
        parts = [p for p in link.strip('/').split('/') if p]
        msg_id = int(parts[-1])
        # Detect if it's a private or public link to find the chat_id
        chat_id = int(f"-100{parts[parts.index('c')+1]}") if 'c' in parts else parts[-2]
        
        message = await client.get_messages(chat_id, ids=msg_id)
        if not message or not message.file:
            print("❌ No video found in this link.")
            return

        filename = message.file.name or f"lecture_{msg_id}.mp4"
        title = get_lecture_title(filename)
        
        # High Speed Download
        downloader = FastDownloader(client, message, filename)
        await downloader.download()
        
        # Upload
        upload_to_youtube(filename, title)
        
        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        print(f"❌ Error: {e}")

async def main():
    if len(sys.argv) < 2: return
    links = sys.argv[1].split(',')
    
    client = TelegramClient(
        'lecture_bot', 
        os.environ['TG_API_ID'], 
        os.environ['TG_API_HASH']
    )
    
    async with client:
        for link in links:
            await process_link(client, link)

if __name__ == "__main__":
    asyncio.run(main())
