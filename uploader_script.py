import os
import sys
import asyncio
import re
from telethon import TelegramClient
from telethon.sessions import StringSession 
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import googleapiclient.errors

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

# Fetching API Keys
# Note: GEMINI and OMDB keys are no longer needed for simple mode

def download_progress_callback(current, total):
    # Simple progress indicator
    if total:
        print(f"⬇️ Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.1f}%)", end='\r')

def get_simple_metadata(message, filename):
    """
    Extracts simple title from filename and description from the Telegram message caption.
    """
    # 1. Title from Filename
    clean_name = os.path.splitext(filename)[0]
    # Replace common separators with spaces and strip
    title = clean_name.replace('_', ' ').replace('.', ' ').strip()
    
    # Ensure title isn't too long for YouTube (max 100 chars)
    if len(title) > 95:
        title = title[:95]

    # 2. Description from Message Caption
    description = message.message if message.message else f"Uploaded from Telegram: {title}"
    
    # 3. Tags (Static)
    tags = ["Telegram", "Video", "Upload"]

    return {
        "title": title,
        "description": description,
        "tags": tags
    }

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
                'categoryId': '22' # 'People & Blogs' as a generic default
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading: {body['snippet']['title']}")
        # Resumable upload allows for more stability with large files
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
            print("\n❌ API LIMIT REACHED!")
            return "LIMIT_REACHED"
        print(f"\n🔴 YouTube HTTP Error: {e}")
        return False
    except Exception as e:
        print(f"\n🔴 Error during upload: {e}")
        return False

def parse_telegram_link(link):
    """
    Parses a Telegram link to extract chat entity and message ID.
    Supports:
    - Public: https://t.me/username/123
    - Private: https://t.me/c/1234567890/123
    """
    link = link.strip()
    # Remove query parameters (e.g., ?single)
    if '?' in link:
        link = link.split('?')[0]
    
    # Regex for Private Links (c/123456/789)
    # Note: Telegram private link IDs usually need -100 prefix for Telethon
    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        chat_id_str = private_match.group(1)
        msg_id = int(private_match.group(2))
        chat_id = int(f"-100{chat_id_str}")
        return chat_id, msg_id

    # Regex for Public Links (username/789)
    public_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if public_match:
        username = public_match.group(1)
        msg_id = int(public_match.group(2))
        return username, msg_id
        
    return None, None

async def process_single_link(client, link):
    try:
        print(f"\n--- Processing: {link} ---")
        
        chat_id, msg_id = parse_telegram_link(link)
        
        if not chat_id or not msg_id:
            print(f"❌ Invalid Link Format: {link}")
            return True

        # Fetch Message
        try:
            message = await client.get_messages(chat_id, ids=msg_id)
        except ValueError:
            # Sometimes private channels need to be accessed differently if not in cache
            # But the user account must be joined to the channel
            print(f"❌ Cannot access chat. Ensure the account is a member of: {chat_id}")
            return True
        except Exception as e:
            print(f"❌ Error fetching message: {e}")
            return True
        
        if not message or not message.media:
            print("❌ No media found in message.")
            return True

        # Determine filename
        if hasattr(message.file, 'name') and message.file.name:
            original_filename = message.file.name
        else:
            ext = message.file.ext if hasattr(message, 'file') else ".mp4"
            original_filename = f"video_{msg_id}{ext}"

        raw_file = f"download_{msg_id}_{original_filename}"
        
        # SAFETY CHECK: Remove file if it exists
        if os.path.exists(raw_file):
            os.remove(raw_file)

        print(f"⬇️ Downloading: {original_filename}")
        await client.download_media(message, raw_file, progress_callback=download_progress_callback)
        print("\n✅ Download Complete.")
        
        # Get Metadata
        metadata = get_simple_metadata(message, original_filename)
        
        # Upload
        status = upload_to_youtube(raw_file, metadata)

        # Cleanup
        if os.path.exists(raw_file):
            os.remove(raw_file)
            
        return status

    except Exception as e:
        print(f"🔴 Critical Error processing link {link}: {e}")
        return False

async def run_flow(links_str):
    links = [l.strip() for l in links_str.split(',') if l.strip()]
    
    try:
        client = TelegramClient(
            StringSession(os.environ['TG_SESSION_STRING']), 
            int(os.environ['TG_API_ID']), 
            os.environ['TG_API_HASH'],
            connection_retries=None,
            retry_delay=5
        )
        await client.start()
        
        for link in links:
            result = await process_single_link(client, link)
            if result == "LIMIT_REACHED":
                break
                
        await client.disconnect()
    except Exception as e:
        print(f"🔴 Client Error: {e}")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
