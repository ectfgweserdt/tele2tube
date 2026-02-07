import os
import json
import asyncio
from google import genai
from telethon import TelegramClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm

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
    """Uses the new Google GenAI SDK to generate metadata."""
    if not text_content:
        text_content = "No description provided. Analyze the context of a generic tuition class."
    
    # Initialize the new Client
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are an intelligent assistant organizing tuition videos.
    Analyze this raw text from a Telegram message: "{text_content}"

    Extract/Generate:
    1. A clear, professional Video Title.
    2. A short Description.
    3. A general Subject Category (e.g., Mechanics, Calculus, Organic Chemistry). 
       Keep the category broad enough to be a Playlist name.

    Return ONLY a JSON object with keys: "title", "description", "category".
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt
        )
        
        # Clean up code blocks if Gemini adds them
        text_response = response.text
        clean_json = text_response.replace('```json', '').replace('```', '').strip()
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
    # Search existing
    request = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
    response = request.execute()
    
    for item in response.get('items', []):
        if item['snippet']['title'].lower() == title.lower():
            return item['id']

    # Create new
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
          "snippet": {"title": title, "description": f"Auto-generated playlist for {title}"},
          "status": {"privacyStatus": "private"}
        }
    )
    response = request.execute()
    return response['id']

async def parse_telegram_link(client, link):
    """
    Robustly parses Telegram links including Topics and Private Channels.
    Logic: The last number in the URL is ALWAYS the message ID.
    """
    clean_link = link.strip().replace('https://', '').replace('http://', '').replace('t.me/', '')
    parts = clean_link.split('/')
    
    # parts examples:
    # Public: ['username', '123'] -> Msg 123
    # Public Topic: ['username', '111', '123'] -> Topic 111, Msg 123
    # Private: ['c', '100123456', '123'] -> Msg 123
    # Private Topic: ['c', '100123456', '111', '123'] -> Topic 111, Msg 123
    
    if len(parts) < 2:
        raise ValueError(f"Invalid link format: {link}")

    msg_id = int(parts[-1]) # Last part is always Message ID
    entity = None

    if parts[0] == 'c':
        # Private Channel
        # Telethon needs -100 prefix for channel IDs if not already present
        # but parts[1] usually is just the number '179...'
        channel_id_str = parts[1]
        channel_id = int(f"-100{channel_id_str}")
        
        try:
            entity = await client.get_entity(channel_id)
        except ValueError:
            # Fallback: iterate dialogs if not in cache
            print(f"Entity {channel_id} not found in cache. Scanning dialogs...")
            async for dialog in client.iter_dialogs():
                if dialog.id == channel_id:
                    entity = dialog.entity
                    break
    else:
        # Public Channel
        username = parts[0]
        try:
            entity = await client.get_entity(username)
        except Exception as e:
            print(f"Could not resolve username {username}. Error: {e}")

    if not entity:
        raise ValueError(f"Could not find entity for link: {link}. Ensure Bot is a member or link is correct.")

    return entity, msg_id

# --- Main Logic ---

async def main():
    if not VIDEO_LINKS or VIDEO_LINKS == ['']:
        print("No video links provided.")
        return

    print("Connecting to Telegram...")
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
            # 1. Resolve Telegram Message using robust parser
            entity, msg_id = await parse_telegram_link(client, link)
            print(f"Resolved: Entity={entity.id if hasattr(entity, 'id') else 'Unknown'}, Message ID={msg_id}")
            
            message = await client.get_messages(entity, ids=msg_id)

            if not message or not message.media:
                print("No media found in message.")
                continue

            # 2. Analyze Content (Metadata)
            original_text = message.text or message.caption or "Tuition Video"
            print("Analyzing content with Gemini...")
            metadata = analyze_content(original_text)
            print(f"Generated Metadata: {json.dumps(metadata, indent=2)}")

            # 3. Download Video
            print("Downloading from Telegram...")
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
                    print(f"Playlist Error: {e}")

            # 6. Cleanup
            if os.path.exists(file_path):
                os.remove(file_path)
                print("Local file cleaned up.")

        except Exception as e:
            print(f"FAILED to process link {link}: {e}")
            import traceback
            traceback.print_exc()

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
