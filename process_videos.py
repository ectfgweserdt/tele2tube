import os
import json
import asyncio
import time
import re
from google import genai
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Configuration ---
TG_API_ID = os.environ.get('TG_API_ID')
TG_API_HASH = os.environ.get('TG_API_HASH')
TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')

YOUTUBE_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
VIDEO_LINKS = os.environ.get('VIDEO_LINKS', '').split(',')

# --- Helpers ---

def get_youtube_service():
    creds = Credentials(
        None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET
    )
    return build('youtube', 'v3', credentials=creds)

def get_or_create_playlist(youtube, title):
    """Finds a playlist by title or creates it."""
    try:
        request = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
        response = request.execute()
        
        for item in response.get('items', []):
            if item['snippet']['title'].lower() == title.lower():
                return item['id']

        print(f"Creating new playlist: {title}")
        request = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": f"Auto-categorized playlist for {title}"},
                "status": {"privacyStatus": "private"}
            }
        )
        response = request.execute()
        return response['id']
    except Exception as e:
        print(f"Playlist error: {e}")
        return None

async def analyze_content_with_retry(text_content):
    """Uses Gemini API with translation capabilities and retries."""
    if not text_content:
        text_content = "Untitled Tuition Video"
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Improved prompt for Sinhala translation and categorization
    prompt = f"""
    You are a classroom assistant. Analyze this Telegram message text: "{text_content}"
    
    Task:
    1. Translate Sinhala terms to English (e.g., 'තාපය' -> 'Heat', 'යාන්ත්‍ර විද්‍යාව' -> 'Mechanics').
    2. Create a professional YouTube Video Title.
    3. Categorize it into a broad Unit/Subject (e.g., Heat, Mechanics, Calculus, Waves). This will be used as a playlist name.
    
    Output MUST be a valid JSON object:
    {{"title": "English Title", "description": "Original text and summary", "category": "Unit Name"}}
    """
    
    models_to_try = ['gemini-2.0-flash', 'gemini-1.5-flash']
    
    for model_name in models_to_try:
        for i in range(5): # Exponential backoff retries
            try:
                response = client.models.generate_content(model=model_name, contents=prompt)
                # Extract JSON from potential markdown markers
                raw_text = response.text
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if match:
                    return json.loads(match.group())
                return json.loads(raw_text)
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    wait = (i + 1) * 10 
                    print(f"Quota hit. Waiting {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    break
    
    # Enhanced local fallback if AI fails completely
    clean_title = text_content.replace('*', '').strip()
    return {
        "title": clean_title if clean_title else f"Video {time.strftime('%Y-%m-%d')}", 
        "description": f"Original caption: {text_content}", 
        "category": "General Tuition"
    }

def progress_callback(current, total):
    percent = (current * 100 / total)
    if int(percent) % 5 == 0:
        print(f"\rDownloading: {percent:.1f}%", end="", flush=True)

async def parse_telegram_link(client, link):
    clean_link = link.strip().replace('https://', '').replace('http://', '').replace('t.me/', '')
    parts = clean_link.split('/')
    msg_id = int(parts[-1])
    entity_id = parts[1] if parts[0] == 'c' else parts[0]
    if parts[0] == 'c':
        entity_id = int(f"-100{entity_id}")
    
    try:
        entity = await client.get_entity(entity_id)
        return entity, msg_id
    except:
        async for dialog in client.iter_dialogs():
            if str(dialog.id) in [str(entity_id), f"-100{entity_id}"]:
                return dialog.entity, msg_id
    raise ValueError(f"Entity not found for {link}")

# --- Main Logic ---

async def main():
    if not VIDEO_LINKS or not VIDEO_LINKS[0]:
        print("No links found.")
        return

    print("Connecting to Telegram...")
    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("Telegram Auth Failed.")
        return

    youtube = get_youtube_service()

    for link in VIDEO_LINKS:
        link = link.strip()
        if not link: continue
        print(f"\n--- Processing: {link} ---")

        try:
            entity, msg_id = await parse_telegram_link(client, link)
            message = await client.get_messages(entity, ids=msg_id)

            if not message or not message.media:
                print("No media found.")
                continue

            print("Analyzing with AI...")
            metadata = await analyze_content_with_retry(message.text or message.caption)
            print(f"Result: {metadata['title']} [{metadata['category']}]")
            
            print("Downloading...")
            if not os.path.exists("downloads"): os.makedirs("downloads")
            file_path = await client.download_media(message, file="downloads/", progress_callback=progress_callback)
            
            print(f"\nUploading to YouTube...")
            body = {
                'snippet': {
                    'title': metadata['title'], 
                    'description': metadata['description'], 
                    'categoryId': '27'
                },
                'status': {'privacyStatus': 'private'}
            }

            media = MediaFileUpload(file_path, chunksize=10*1024*1024, resumable=True)
            upload_request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

            response = None
            while response is None:
                status, response = upload_request.next_chunk()
                if status:
                    print(f"\rUpload Progress: {int(status.progress() * 100)}%", end="", flush=True)

            video_id = response.get('id')
            print(f"\nSuccess! Video ID: {video_id}")

            # Playlist Management
            playlist_id = get_or_create_playlist(youtube, metadata['category'])
            if playlist_id:
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}
                        }
                    }
                ).execute()
                print(f"Added to playlist: {metadata['category']}")

            if os.path.exists(file_path): os.remove(file_path)

        except Exception as e:
            print(f"Error processing link: {e}")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
