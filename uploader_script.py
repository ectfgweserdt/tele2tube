import os
import sys
import time
import asyncio
import math
from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# --- CONFIGURATION ---
PARALLEL_CONNECTIONS = 15 
CHUNK_SIZE = 1024 * 1024  

class FastDownloader:
    def __init__(self, client, message, file_path):
        self.client = client
        self.message = message
        self.file_path = file_path
        self.total_size = message.file.size
        self.downloaded = 0
        self._last_print = 0

    async def download_part(self, offset, limit, part_index):
        part_path = f"{self.file_path}.part{part_index}"
        try:
            async for chunk in self.client.iter_download(self.message.document, offset=offset, limit=limit):
                with open(part_path, "ab") as f:
                    f.write(chunk)
                    self.downloaded += len(chunk)
                    
                    if time.time() - self._last_print > 5:
                        percent = (self.downloaded / self.total_size) * 100
                        print(f"🚀 Downloading: {percent:.2f}% ({self.downloaded // 1024 // 1024}MB / {self.total_size // 1024 // 1024}MB)")
                        self._last_print = time.time()
        except Exception as e:
            print(f"⚠️ Chunk {part_index} error: {e}")

    async def download(self):
        part_size = math.ceil(self.total_size / PARALLEL_CONNECTIONS)
        tasks = []
        for i in range(PARALLEL_CONNECTIONS):
            offset = i * part_size
            limit = min(part_size, self.total_size - offset)
            if limit <= 0: break
            tasks.append(self.download_part(offset, limit, i))
        
        await asyncio.gather(*tasks)
        
        with open(self.file_path, "wb") as final_file:
            for i in range(PARALLEL_CONNECTIONS):
                part_name = f"{self.file_path}.part{i}"
                if os.path.exists(part_name):
                    with open(part_name, "rb") as pf:
                        final_file.write(pf.read())
                    os.remove(part_name)
        print(f"✅ File Saved Locally: {self.file_path}")

def get_lecture_title(filename):
    name = os.path.splitext(filename)[0]
    # Clean up common characters
    title = name.replace("_", " ").replace(".", " ").replace("-", " ")
    return title.title()

def upload_to_youtube(file_path, title):
    try:
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
                "description": f"Class Lecture: {title}\nAutomated Archive.", 
                "categoryId": "27"
            },
            "status": {"privacyStatus": "private"}
        }
        media = MediaFileUpload(file_path, chunksize=1024*1024*5, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        print(f"📤 Starting YouTube Upload: {title}")
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"📈 YouTube Progress: {int(status.progress() * 100)}%")
        print(f"🎉 YouTube ID: {response['id']}")
    except Exception as e:
        print(f"❌ YouTube Upload Error: {e}")

async def process_link(client, link):
    try:
        print(f"\n🔗 Analyzing Link: {link}")
        parts = [p for p in link.strip('/').split('/') if p]
        
        # Link Format Handling (t.me/c/CHANNEL_ID/TOPIC_ID/MSG_ID)
        msg_id = int(parts[-1])
        if 'c' in parts:
            c_idx = parts.index('c')
            # The channel ID is immediately after 'c'
            chat_id = int(f"-100{parts[c_idx + 1]}")
        else:
            # Public links t.me/CHANNEL_NAME/MSG_ID
            chat_id = parts[-2]

        print(f"📡 Fetching from Chat: {chat_id}, Message: {msg_id}")
        message = await client.get_messages(chat_id, ids=msg_id)
        
        if not message or not message.file:
            print("❌ No video file found in this message. Check if you have access to this chat.")
            return

        filename = message.file.name or f"lecture_{msg_id}.mp4"
        title = get_lecture_title(filename)
        
        print(f"📂 File identified: {filename}")
        downloader = FastDownloader(client, message, filename)
        await downloader.download()
        
        upload_to_youtube(filename, title)
        
        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        print(f"❌ Critical Error in process_link: {e}")

async def main():
    print("🚀 Script Initialization...")
    if len(sys.argv) < 2:
        print("❌ Error: No links provided as arguments.")
        return
    
    links = sys.argv[1].split(',')
    
    api_id = os.environ.get('TG_API_ID')
    api_hash = os.environ.get('TG_API_HASH')
    session_str = os.environ.get('TG_SESSION_STRING', '').strip()

    if not session_str:
        print("❌ Error: TG_SESSION_STRING is missing from GitHub Secrets or Environment Variables.")
        return

    print("🛰 Connecting to Telegram via User Session...")
    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ Authentication Failed: The TG_SESSION_STRING is invalid or has expired.")
            return
            
        me = await client.get_me()
        print(f"✅ Logged in as: {me.first_name} (@{me.username})")

        for link in links:
            if link.strip():
                await process_link(client, link.strip())
                
    except Exception as e:
        print(f"❌ Main Execution Loop Error: {e}")
    finally:
        await client.disconnect()
        print("🔌 Disconnected from Telegram.")

if __name__ == "__main__":
    asyncio.run(main())
