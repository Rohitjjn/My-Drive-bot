import os
import time
import math
import asyncio
import logging
import threading
import json
import traceback
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from flask import Flask

# --- 1. LOGGING SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. CONFIG VARIABLES ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
GDRIVE_TOKEN_CONTENT = os.environ.get("GDRIVE_TOKEN")

# --- 3. GOOGLE DRIVE AUTH (PERSONAL TOKEN) ---
# Hum seedha Token string use karenge, file banane ki zaroorat nahi
try:
    token_dict = json.loads(GDRIVE_TOKEN_CONTENT)
    creds = Credentials.from_authorized_user_info(token_dict)
    drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    print(f"❌ Auth Error: {e}")

# --- 4. BOT SETUP ---
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
upload_semaphore = asyncio.Semaphore(4)

# --- HELPERS ---
def humanbytes(size):
    if not size: return ""
    power = 2**10
    n = 0
    dic_powerN = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + dic_powerN[n] + 'B'

def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days}d, {hours}h, {minutes}m, {seconds}s" if days else f"{hours}h, {minutes}m, {seconds}s"

async def progress_func(current, total, start_time, status_msg):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff
        elapsed_time = round(diff) * 1000
        estimated_total_time = elapsed_time + round((total - current) / speed) * 1000
        
        progress_str = "[{0}{1}] {2}%".format(
            ''.join(["●" for i in range(math.floor(percentage / 10))]),
            ''.join(["○" for i in range(10 - math.floor(percentage / 10))]),
            round(percentage, 2))
        
        tmp = f"{progress_str}\n📦 {humanbytes(current)} of {humanbytes(total)}\n🚀 {humanbytes(speed)}/s\n⏱️ ETA: {time_formatter(estimated_total_time)}"
        try:
            await status_msg.edit(f"📥 **Downloading...**\n\n{tmp}")
        except: pass

# --- STATS COMMAND ---
@app.on_message(filters.command("stats") & filters.user(ADMIN_ID))
async def stats_handler(client, message):
    try:
        about = drive_service.about().get(fields="storageQuota").execute()
        quota = about.get('storageQuota', {})
        limit = int(quota.get('limit', 0))
        usage = int(quota.get('usage', 0))
        percent = (usage / limit) * 100 if limit > 0 else 0
        
        await message.reply_text(
            f"📊 **Personal Drive Stats:**\n\n"
            f"💿 **Total:** {humanbytes(limit)}\n"
            f"📦 **Used:** {humanbytes(usage)} ({round(percent, 2)}%)\n"
            f"✅ **Account:** Authorized via Token"
        )
    except Exception as e:
        await message.reply_text(f"❌ Error fetching stats: {e}")

# --- UPLOAD HANDLER ---
@app.on_message(filters.user(ADMIN_ID) & (filters.document | filters.video))
async def upload_handler(client, message):
    status_msg = await message.reply_text("⏳ **Queue...**")
    
    async with upload_semaphore:
        await status_msg.edit("🚀 **Processing...**")
        
        file_name = "unknown"
        mime_type = "video/mp4"
        if message.video:
            file_name = message.video.file_name or "video.mp4"
            mime_type = message.video.mime_type
        elif message.document:
            file_name = message.document.file_name
            mime_type = message.document.mime_type
            
        if not os.path.exists("downloads"): os.makedirs("downloads")
        save_path = f"downloads/{file_name}"
        
        try:
            # 1. Download
            start_time = time.time()
            await message.download(save_path, progress=progress_func, progress_args=(start_time, status_msg))
            
            # 2. Upload
            await status_msg.edit("📤 **Uploading to Personal Drive...**")
            
            file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
            media = MediaIoBaseUpload(open(save_path, 'rb'), mimetype=mime_type, resumable=True)
            
            file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webContentLink').execute()
            
            # 3. Cleanup
            if os.path.exists(save_path): os.remove(save_path)
            
            # 4. Success Message
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete from Drive", callback_data=f"del_{file.get('id')}") ]])
            await status_msg.edit(f"✅ **Upload Complete!**\n\n📂 `{file_name}`\n🔗 [Download Link]({file.get('webContentLink')})", reply_markup=keyboard)
            
        except Exception as e:
            await status_msg.edit(f"❌ **Error:** {e}")
            if os.path.exists(save_path): os.remove(save_path)

# --- DELETE BUTTON ---
@app.on_callback_query(filters.regex(r"^del_"))
async def delete_callback(client, callback_query):
    file_id = callback_query.data.split("_")[1]
    try:
        drive_service.files().delete(fileId=file_id).execute()
        await callback_query.answer("Deleted!", show_alert=True)
        await callback_query.message.edit_text("🗑️ **File Permanently Deleted.**")
    except Exception as e:
        await callback_query.answer(f"Error: {e}", show_alert=True)

# --- FAKE SERVER (KEEP ALIVE) ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot is Running via Personal Token!"
def run_flask(): flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- START ---
if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    print("Bot Started...")
    app.run()
    
