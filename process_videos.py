import os
import json
import asyncio
import time
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

async def analyze_content_with_retry(text_content):
    """Uses Gemini API with backoff and model fallbacks."""
    if not text_content:
        text_content = "No description provided. Analyze the context of a generic tuition class."
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are an intelligent assistant organizing tuition videos.
    Analyze this text: "{text_content}"
    Return ONLY a JSON object with keys: "title", "description", "category".
    """
    
    # Try these models in order
    models_to_try = ['gemini-2.0-flash', 'gemini-1.5-flash']
    
    for model_name in models_to_try:
        retries = 3
        for i in range(retries):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                clean_json = response.text.replace('```json', '').replace('```', '').strip()
                return json.loads(clean_json)
            except Exception as e:
                # If it's a 404, stop retrying this model and move to the next one
                if "404" in str(e):
                    break 
                if i < retries - 1:
                    await asyncio.sleep(2 ** i)
                    continue
    
    # Final Fallback if all AI attempts fail
    return {
        "title": f"Class Video - {time.strftime('%Y-%m-%d')}", 
        "description": f"Original text: {text_content}", 
        "category": "General Tuition"
    }

def progress_callback(current, total):
    # Only print every 5% to keep the GitHub log clean and fast
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
                continue

            print("Analyzing with AI...")
            metadata = await analyze_content_with_retry(message.text or message.caption)
            
            print("Downloading...")
            if not os.path.exists("downloads"): os.makedirs("downloads")
            file_path = await client.download_media(message, file="downloads/", progress_callback=progress_callback)
            
            print(f"\nUploading to YouTube: {metadata['title']}")
            body = {
                'snippet': {'title': metadata['title'], 'description': metadata['description'], 'categoryId': '27'},
                'status': {'privacyStatus': 'private'}
            }

            media = MediaFileUpload(file_path, chunksize=10*1024*1024, resumable=True)
            upload_request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

            response = None
            while response is None:
                status, response = upload_request.next_chunk()
                if status:
                    print(f"\rUpload: {int(status.progress() * 100)}%", end="", flush=True)

            print(f"\nSuccess! ID: {response.get('id')}")
            if os.path.exists(file_path): os.remove(file_path)

        except Exception as e:
            print(f"Error: {e}")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
