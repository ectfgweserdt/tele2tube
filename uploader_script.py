import os
import sys
import argparse
import time
import asyncio
from telethon import TelegramClient
# FIX: 'MessageMediaVideo' is no longer available in newer Telethon versions.
# We now rely only on MessageMediaDocument, which typically encapsulates videos too.
from telethon.tl.types import MessageMediaDocument
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# --- CONFIGURATION ---
# YouTube Scopes required for video upload
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

# --- TELEGRAM LINK UTILITY ---
def parse_telegram_link(link):
    """Parses a t.me/c/CHAT_ID/MSG_ID link into parts."""
    try:
        # Expected format: https://t.me/c/1234567890/12345
        parts = link.strip('/').split('/')
        if len(parts) < 2 or parts[-2] != 'c':
            raise ValueError("Link must be a canonical message link, e.g., 'https://t.me/c/CHAT_ID/MSG_ID'")
        
        # Telegram client requires chat ID to be negative if it's a supergroup
        channel_id = int(parts[-2] + parts[-3])
        message_id = int(parts[-1])
        return channel_id, message_id
    except Exception as e:
        print(f"Error parsing link: {e}")
        sys.exit(1)

# --- YOUTUBE AUTHENTICATION (FOR GITHUB WORKFLOW) ---
def get_youtube_service(client_id, client_secret, refresh_token):
    """Authenticates using a stored refresh token for non-interactive use."""
    print("Authenticating with YouTube using Refresh Token...")
    try:
        # Create a mock credentials object using the refresh token
        creds = Credentials(
            token=None,  # No immediate access token needed, it will be refreshed
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
            scopes=YOUTUBE_SCOPES
        )
        # Attempt to refresh the token to get a valid service
        creds.refresh(Request())
        
        # Build the YouTube service client
        youtube = build('youtube', 'v3', credentials=creds)
        print("YouTube Authentication successful.")
        return youtube
    except Exception as e:
        print(f"YouTube Authentication Error. Check CLIENT_ID, CLIENT_SECRET, and REFRESH_TOKEN: {e}")
        sys.exit(1)

# --- YOUTUBE UPLOAD ---
def upload_video(youtube, filepath, title, description):
    """Uploads the video file and sets its privacy status to private."""
    print(f"Starting upload for: {title}")
    
    body = dict(
        snippet=dict(
            title=title,
            description=description,
            tags=["educational", "telegram_export"],
            categoryId="27" # Category 27 is "Education"
        ),
        status=dict(
            privacyStatus='private' # This is the crucial step to make it private
        )
    )

    media = MediaFileUpload(filepath, chunksize=-1, resumable=True)
    
    # Insert request (resumable upload handled by the client library)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    error = None
    retry = 0
    MAX_RETRIES = 5
    
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"Uploaded {int(status.progress() * 100)}%")
            
            if response is not None:
                if 'id' in response:
                    print(f"✅ Video Upload Complete! YouTube ID: {response['id']}")
                    print(f"Link: https://www.youtube.com/watch?v={response['id']}")
                    return response['id']
                else:
                    raise Exception(f"Upload failed with unexpected response: {response}")

        except Exception as e:
            error = e
            retry += 1
            if retry > MAX_RETRIES:
                print(f"🔴 Fatal Error: Maximum retries reached. Upload failed. {error}")
                break
            
            # Simple exponential backoff
            sleep_time = 2 ** retry
            print(f"Retriable error occurred: {error}. Retrying in {sleep_time} seconds...")
            time.sleep(sleep_time)
            
    return None

# --- TELEGRAM DOWNLOAD ---
async def download_video_and_upload(link):
    """Main asynchronous function to handle the Telegram download and YouTube upload."""
    
    # 1. Get secrets from environment variables (set by GitHub Actions)
    TG_API_ID = os.environ.get('TG_API_ID')
    TG_API_HASH = os.environ.get('TG_API_HASH')
    TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')
    
    YT_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
    YT_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
    YT_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

    if not all([TG_API_ID, TG_API_HASH, TG_SESSION_STRING, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        print("🔴 Missing one or more required secrets. Please check your GitHub Secrets setup.")
        # If running locally to generate the session string, only the TG secrets are required
        if not TG_SESSION_STRING:
            print("\n--- INITIATING TELEGRAM SESSION GENERATOR ---\n")
            await generate_telegram_session(TG_API_ID, TG_API_HASH)
            return
        
        sys.exit(1)

    # 2. Parse the input link
    channel_id, message_id = parse_telegram_link(link)
    print(f"Targeting channel ID: {channel_id}, Message ID: {message_id}")
    
    client = None
    downloaded_filepath = None
    try:
        # 3. Connect to Telegram
        print("Connecting to Telegram...")
        # Use the session string for non-interactive login
        client = TelegramClient(TG_SESSION_STRING, TG_API_ID, TG_API_HASH)
        await client.start()
        print("Connection successful.")

        # 4. Get the message
        print(f"Fetching message {message_id} from chat {channel_id}...")
        message = await client.get_messages(channel_id, ids=message_id)

        # Updated check: MessageMediaVideo is removed, relying on MessageMediaDocument to cover videos.
        if not message or not (message.media and isinstance(message.media, MessageMediaDocument)):
            print("🔴 Error: Message is missing or does not contain a supported media file (video/document).")
            return

        # 5. Download the file
        file_name = f"video_{channel_id}_{message_id}.mp4"
        print(f"Downloading file to {file_name}...")
        downloaded_filepath = await client.download_media(message, file_name)
        print(f"✅ Download complete: {downloaded_filepath}")
        
        # Determine Title and Description
        # Use the message text as the video description, and the file name as the title
        title = os.path.basename(downloaded_filepath)
        description = message.message if message.message else f"Exported video from Telegram message {link}"
        
        # 6. YouTube Authentication and Upload
        youtube_service = get_youtube_service(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN)
        upload_video(youtube_service, downloaded_filepath, title, description)

    except Exception as e:
        print(f"An unexpected error occurred during the process: {e}")
    
    finally:
        # 7. Cleanup
        if client:
            await client.disconnect()
        if downloaded_filepath and os.path.exists(downloaded_filepath):
            print(f"Cleaning up local file: {downloaded_filepath}")
            os.remove(downloaded_filepath)

# --- LOCAL SESSION GENERATION (Run this once locally) ---
async def generate_telegram_session(api_id, api_hash):
    """
    Runs locally to generate the TG_SESSION_STRING for use in GitHub secrets.
    Requires TG_API_ID and TG_API_HASH to be set locally or passed in.
    """
    if not api_id or not api_hash:
        print("TG_API_ID and TG_API_HASH must be provided to generate a session string.")
        return

    # Use a fixed session name 'temp_session'
    client = TelegramClient('temp_session', api_id, api_hash)
    await client.start()
    
    print("\n-------------------------------------------------------------")
    print("      🔑 TELEGRAM SESSION STRING GENERATED 🔑")
    print("-------------------------------------------------------------")
    
    # Get the session string and print it clearly for the user to copy
    session_string = client.session.save()
    print("\nCOPY THIS ENTIRE STRING AND SAVE IT AS 'TG_SESSION_STRING' IN GITHUB SECRETS:")
    print(session_string)
    print("\n-------------------------------------------------------------")
    
    await client.disconnect()
    # Cleanup local files that might be created by Telethon
    if os.path.exists('temp_session.session'):
        os.remove('temp_session.session')

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Automated Telegram video downloader and YouTube uploader.')
    parser.add_argument('telegram_link', nargs='?', help='The full URL of the Telegram message/video (e.g., https://t.me/c/ID/MSG_ID).')
    args = parser.parse_args()

    # If the link is missing, we assume the user is trying to generate the session string locally
    if not args.telegram_link:
        print("No Telegram link provided. Checking for secrets to initiate session generation...")
        
        # Attempt to get local env variables for session generation
        local_api_id = os.environ.get('TG_API_ID') or os.environ.get('TELEGRAM_API_ID') 
        local_api_hash = os.environ.get('TG_API_HASH') or os.environ.get('TELEGRAM_API_HASH')

        if local_api_id and local_api_hash:
             # Run session generation asynchronously
            asyncio.run(generate_telegram_session(local_api_id, local_api_hash))
            print("Session generation finished. You must copy the string above and set it as a GitHub Secret.")
            print("\nNext, run the script again with the telegram link argument from GitHub Actions.")
        else:
            print("To generate the session string locally, you must set the TG_API_ID and TG_API_HASH environment variables first.")
        
    else:
        # If the link is provided, run the full process asynchronously
        asyncio.run(download_video_and_upload(args.telegram_link))
