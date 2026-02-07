import os
import sys
import json
import asyncio
import google.generativeai as genai
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from tqdm import tqdm

# --- Configuration ---
# Telegram Secrets
TG_API_ID = os.environ.get('TG_API_ID')
TG_API_HASH = os.environ.get('TG_API_HASH')
TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')

# YouTube Secrets
YOUTUBE_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

# Gemini Secrets
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Inputs
VIDEO_LINKS = os.environ.get('VIDEO_LINKS', '').split(',')

# --- Helpers ---

def get_youtube_service():
    """Authenticates with YouTube using a Refresh Token."""
    creds = Credentials(
        None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET
    )
    return build('youtube', 'v3', credentials=creds)

def analyze_content(text_content):
    """Uses Gemini to generate Title, Description, and Category."""
    if not text_content:
        text_content = "No description provided. Analyze the context of a generic tuition class."
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    prompt = f"""
    You are an intelligent assistant organizing tuition videos.
    Analyze this raw text from a Telegram message: "{text_content}"

    Extract/Generate:
    1. A clear, professional Video Title.
    2. A short Description.
    3. A general Subject Category (e.g., Mechanics, Calculus, Organic Chemistry, Electronics). 
       Keep the category broad enough to be a Playlist name.

    Return ONLY a JSON object with keys: "title", "description", "category".
    """
    
    try:
        response = model.generate_content(prompt)
        # Clean up code blocks if Gemini adds them
        clean_json = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_json)
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {
            "title": "Tuition Class Video", 
            "description": f"Original text: {text_content}", 
            "category": "General Tuition"
        }

def progress_callback(current, total):
    print(f"\rDownloading: {current * 100 / total:.1f}%", end="")

def get_or_create_playlist(youtube, title):
    """Finds a playlist by title or creates it."""
    # 1. Search existing playlists
    request = youtube.playlists().list(
        part="snippet",
        mine=True,
        maxResults=50
    )
    response = request.execute()
    
    for item in response.get('items', []):
        if item['snippet']['title'].lower() == title.lower():
            print(f"Found existing playlist: {title}")
            return item['id']

    # 2. Create if not exists
    print(f"Creating new playlist: {title}")
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
          "snippet": {
            "title": title,
            "description": f"Auto-generated playlist for {title}"
          },
          "status": {
            "privacyStatus": "private"
          }
        }
    )
    response = request.execute()
    return response['id']

# --- Main Logic ---

async def main():
    if not VIDEO_LINKS or VIDEO_LINKS == ['']:
        print("No video links provided.")
        return

    print("Connecting to Telegram...")
    client = TelegramClient(
        'bot_session', 
        int(TG_API_ID), 
        TG_API_HASH, 
        system_version='4.16.30-vxCUSTOM'
    )
    
    # Start client using session string
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("Error: Telegram Session Invalid or Expired.")
        return

    youtube = get_youtube_service()

    for link in VIDEO_LINKS:
        link = link.strip()
        if not link: continue
        
        print(f"\n--- Processing: {link} ---")

        try:
            # 1. Resolve Telegram Message
            # Expected format: https://t.me/c/123123123/100 or https://t.me/username/100
            if '/c/' in link:
                # Private channel link parsing
                parts = link.split('/')
                channel_id = int('-100' + parts[-2]) # Telethon needs -100 prefix for channel IDs
                msg_id = int(parts[-1])
                entity = await client.get_entity(channel_id)
                message = await client.get_messages(entity, ids=msg_id)
            else:
                # Public link
                message = await client.get_messages(link)

            if not message or not message.media:
                print("No media found in message.")
                continue

            # 2. Analyze Content (Metadata)
            original_text = message.text or message.caption or "Tuition Video"
            print("Analyzing content with Gemini...")
            metadata = analyze_content(original_text)
            print(f"Generated Metadata: {json.dumps(metadata, indent=2)}")

            # 3. Download Video
            print("Downloading from Telegram (Simple Method)...")
            file_path = await client.download_media(
                message, 
                file="downloads/", 
                progress_callback=progress_callback
            )
            print(f"\nDownloaded to: {file_path}")

            # 4. Upload to YouTube
            print("Uploading to YouTube...")
            body = {
                'snippet': {
                    'title': metadata['title'],
                    'description': metadata['description'],
                    'tags': ['tuition', metadata['category'], 'education'],
                    'categoryId': '27' # Education
                },
                'status': {
                    'privacyStatus': 'private'
                }
            }

            media = MediaFileUpload(file_path, chunksize=1024*1024, resumable=True)
            upload_request = youtube.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )

            response = None
            while response is None:
                status, response = upload_request.next_chunk()
                if status:
                    print(f"\rUpload Progress: {int(status.progress() * 100)}%", end='')

            video_id = response.get('id')
            print(f"\nUpload Complete! Video ID: {video_id}")

            # 5. Add to Playlist
            if video_id:
                try:
                    playlist_id = get_or_create_playlist(youtube, metadata['category'])
                    youtube.playlistItems().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "playlistId": playlist_id,
                                "resourceId": {
                                    "kind": "youtube#video",
                                    "videoId": video_id
                                }
                            }
                        }
                    ).execute()
                    print(f"Added to playlist: {metadata['category']}")
                except Exception as e:
                    print(f"Playlist Error (Video is safe, just not in playlist): {e}")

            # 6. Cleanup
            os.remove(file_path)
            print("Local file cleaned up.")

        except Exception as e:
            print(f"FAILED to process link {link}: {e}")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
