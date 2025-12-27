import os
import sys
import argparse
import time
import asyncio
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ... [Keep parse_telegram_link, get_youtube_service, upload_video as they were] ...

async def process_batch(links_string):
    """Processes a comma-separated list of links sequentially."""
    # Split the input string into a list of clean links
    links = [l.strip() for l in links_string.split(',') if l.strip()]
    
    # Load Secrets
    TG_API_ID = os.environ.get('TG_API_ID')
    TG_API_HASH = os.environ.get('TG_API_HASH')
    TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')
    YT_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
    YT_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
    YT_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

    if not all([TG_API_ID, TG_API_HASH, TG_SESSION_STRING, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        print("🔴 Missing secrets in GitHub Actions.")
        return

    # Initialize YouTube and Telegram once for the whole batch
    youtube_service = get_youtube_service(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN)
    session = StringSession(TG_SESSION_STRING)
    
    async with TelegramClient(session, TG_API_ID, TG_API_HASH) as client:
        print(f"✅ Connected to Telegram. Processing {len(links)} files...")

        for index, link in enumerate(links):
            print(f"\n--- Processing Item {index + 1} of {len(links)} ---")
            channel_id, message_id = parse_telegram_link(link)
            
            if not channel_id:
                continue

            downloaded_filepath = None
            try:
                message = await client.get_messages(channel_id, ids=message_id)
                
                if not message or not (message.media and isinstance(message.media, MessageMediaDocument)):
                    print(f"❌ Skipping: No video found at {link}")
                    continue

                file_name = f"video_{channel_id}_{message_id}.mp4"
                
                # Download
                downloaded_filepath = await client.download_media(
                    message, file_name, progress_callback=download_progress_callback
                )
                print(f"\n✅ Downloaded: {downloaded_filepath}")

                # Upload to YouTube
                title = os.path.basename(downloaded_filepath)
                description = message.message if message.message else f"Exported from {link}"
                upload_video(youtube_service, downloaded_filepath, title, description)

            except Exception as e:
                print(f"🔴 Error processing {link}: {e}")
            
            finally:
                # Clean up file immediately after each upload to save disk space
                if downloaded_filepath and os.path.exists(downloaded_filepath):
                    os.remove(downloaded_filepath)
                    print(f"🗑️ Deleted local file: {downloaded_filepath}")
            
            # Anti-Ban Protection: Small pause between tasks
            if index < len(links) - 1:
                print("Waiting 5 seconds before next file to prevent rate-limiting...")
                await asyncio.sleep(5)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('telegram_link', nargs='?')
    args = parser.parse_args()

    if not args.telegram_link:
        # Run your session generation logic here
        pass
    else:
        asyncio.run(process_batch(args.telegram_link))
