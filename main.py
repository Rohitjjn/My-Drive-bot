import os
import time
import math
import asyncio
import logging
import threading
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from flask import Flask

# --- 1. LOGGING SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. CONFIG VARIABLES (From Render Secrets) ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
GDRIVE_JSON_CONTENT = os.environ.get("GDRIVE_JSON")

# --- 3. GOOGLE DRIVE AUTHENTICATION ---
# JSON content ko file me convert karte hain taaki library use padh sake
CRED_FILE = "token.json"
with open(CRED_FILE, "w") as f:
    f.write(GDRIVE_JSON_CONTENT)

SCOPES = ['https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file(CRED_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

# --- 4. BOT SETUP ---
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- 5. QUEUE SYSTEM (Semaphore) ---
# Ye sunishchit karega ki ek baar me sirf 4 files hi process hon
upload_semaphore = asyncio.Semaphore(4)

# --- HELPER: Human Readable Size ---
def humanbytes(size):
    if not size:
        return ""
    power = 2**10
    n = 0
    dic_powerN = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + dic_powerN[n] + 'B'

# --- HELPER: Time Formatter ---
def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days}d, {hours}h, {minutes}m, {seconds}s" if days else f"{hours}h, {minutes}m, {seconds}s"

# --- PROGRESS BAR FUNCTION ---
async def progress_func(current, total, start_time, status_msg):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total: # Har 5 sec me update
        percentage = current * 100 / total
        speed = current / diff
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000
        estimated_total_time = elapsed_time + time_to_completion
        
        progress_str = "[{0}{1}] {2}%\n".format(
            ''.join(["●" for i in range(math.floor(percentage / 10))]),
            ''.join(["○" for i in range(10 - math.floor(percentage / 10))]),
            round(percentage, 2))
        
        tmp = progress_str + \
              f"**Processed:** {humanbytes(current)} of {humanbytes(total)}\n" + \
              f"**Speed:** {humanbytes(speed)}/s\n" + \
              f"**ETA:** {time_formatter(estimated_total_time)}"
        try:
            await status_msg.edit(f"📥 **Downloading...**\n\n{tmp}")
        except:
            pass

# --- MAIN UPLOAD HANDLER ---
@app.on_message(filters.user(ADMIN_ID) & (filters.document | filters.video))
async def upload_handler(client, message):
    # Queue Start
    status_msg = await message.reply_text("⏳ **Added to Queue...** (Waiting for slot)")
    
    async with upload_semaphore:
        await status_msg.edit("🚀 **Starting Process...**")
        
        # File info nikalna
        file_name = "unknown"
        mime_type = "application/octet-stream"
        
        if message.video:
            file_name = message.video.file_name or "video.mp4"
            mime_type = message.video.mime_type
        elif message.document:
            file_name = message.document.file_name
            mime_type = message.document.mime_type
            
        # Download Path
        if not os.path.exists("downloads"):
            os.makedirs("downloads")
        save_path = f"downloads/{file_name}"
        
        try:
            # 1. DOWNLOAD to Render Server
            start_time = time.time()
            await message.download(
                file_name=save_path,
                progress=progress_func,
                progress_args=(start_time, status_msg)
            )
            
            # 2. UPLOAD to Google Drive
            await status_msg.edit("📤 **Uploading to Google Drive...**\n(Please wait, this may take time)")
            
            file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
            
            # Resumable upload for better handling of large files
            media = MediaIoBaseUpload(open(save_path, 'rb'), mimetype=mime_type, resumable=True)
            
            # Execute upload
            file = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webContentLink'
            ).execute()
            
            drive_file_id = file.get('id')
            drive_link = file.get('webContentLink')
            
            # 3. DELETE Local File (Cleanup)
            os.remove(save_path)
            
            # 4. SEND SUCCESS MESSAGE (With Delete Button)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Delete from Drive", callback_data=f"del_{drive_file_id}")]
            ])
            
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"📂 **Name:** `{file_name}`\n"
                f"🔗 **Link:** [Download Now]({drive_link})",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            
        except Exception as e:
            await status_msg.edit(f"❌ **Error:** {e}")
            # Agar error aaye to bhi local file delete karo
            if os.path.exists(save_path):
                os.remove(save_path)

# --- DELETE BUTTON CALLBACK ---
@app.on_callback_query(filters.regex(r"^del_"))
async def delete_callback(client, callback_query):
    file_id = callback_query.data.split("_")[1]
    try:
        drive_service.files().delete(fileId=file_id).execute()
        await callback_query.answer("File deleted!", show_alert=True)
        # Message update karke button hata do
        await callback_query.message.edit_text("🗑️ **File Permanently Deleted from Drive.**")
    except Exception as e:
        await callback_query.answer(f"Failed to delete: {e}", show_alert=True)

# --- FAKE FLASK SERVER (For Render Free Tier) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is Running Successfully!"

def run_flask():
    # Render automatically assigns a PORT, default to 8080 if not found
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- STARTUP ---
if __name__ == "__main__":
    # Start Web Server in Background Thread
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    # Start Telegram Bot
    print("Bot Started...")
    app.run()
  
