import os
import time
import math
import asyncio
import logging
import threading
import traceback  # Error dekhne ke liye important
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google.oauth2 import service_account
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
GDRIVE_JSON_CONTENT = os.environ.get("GDRIVE_JSON")

# --- 3. GOOGLE DRIVE AUTH ---
CRED_FILE = "token.json"
with open(CRED_FILE, "w") as f:
    f.write(GDRIVE_JSON_CONTENT)

SCOPES = ['https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file(CRED_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

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
        time_to_completion = round((total - current) / speed) * 1000
        estimated_total_time = elapsed_time + time_to_completion
        
        progress_str = "[{0}{1}] {2}%".format(
            ''.join(["●" for i in range(math.floor(percentage / 10))]),
            ''.join(["○" for i in range(10 - math.floor(percentage / 10))]),
            round(percentage, 2))
        
        tmp = f"{progress_str}\n📦 {humanbytes(current)} of {humanbytes(total)}\n🚀 {humanbytes(speed)}/s\n⏱️ ETA: {time_formatter(estimated_total_time)}"
        try:
            await status_msg.edit(f"📥 **Downloading...**\n\n{tmp}")
        except: pass

# --- STATS COMMAND (Ye upar hona chahiye) ---
@app.on_message(filters.command("stats") & filters.user(ADMIN_ID))
async def stats_handler(client, message):
    status_msg = await message.reply_text("📊 **Checking Storage Info...**")
    try:
        about = drive_service.about().get(fields="storageQuota").execute()
        quota = about.get('storageQuota', {})
        
        limit = int(quota.get('limit', 0))
        usage = int(quota.get('usage', 0))
        usage_trash = int(quota.get('usageInDriveTrash', 0))
        
        percent = (usage / limit) * 100 if limit > 0 else 0
        
        text = (
            f"📊 **Drive Status:**\n"
            f"💿 Total: {humanbytes(limit)}\n"
            f"📦 Used: {humanbytes(usage)} ({round(percent, 2)}%)\n"
            f"🗑️ Trash: {humanbytes(usage_trash)}\n\n"
            f"Use /clean to free up space."
        )
        await status_msg.edit(text)
    except Exception as e:
        await status_msg.edit(f"❌ Error: {e}")

# --- CLEAN COMMAND (Space Khali Karne ke liye) ---
@app.on_message(filters.command("clean") & filters.user(ADMIN_ID))
async def clean_handler(client, message):
    status_msg = await message.reply_text("🧹 **Cleaning Trash & Old Files...**")
    try:
        # Trash empty karo
        drive_service.files().emptyTrash().execute()
        
        # Bot ki files delete karo
        results = drive_service.files().list(q="'me' in owners and trashed=false", pageSize=50).execute()
        items = results.get('files', [])
        for file in items:
            try:
                drive_service.files().delete(fileId=file['id']).execute()
            except: pass
            
        await status_msg.edit("✅ **Cleanup Done!** Trash emptied & Files deleted.")
    except Exception as e:
        await status_msg.edit(f"❌ Error: {e}")

# --- MAIN UPLOAD HANDLER ---
@app.on_message(filters.user(ADMIN_ID) & (filters.document | filters.video))
async def upload_handler(client, message):
    status_msg = await message.reply_text("⏳ **In Queue...**")
    
    async with upload_semaphore:
        await status_msg.edit("🚀 **Processing...**")
        
        file_name = "unknown"
        mime_type = "application/octet-stream"
        if message.video:
            file_name = message.video.file_name or "video.mp4"
            mime_type = message.video.mime_type
        elif message.document:
            file_name = message.document.file_name
            mime_type = message.document.mime_type
            
        if not os.path.exists("downloads"): os.makedirs("downloads")
        save_path = f"downloads/{file_name}"
        
        try:
            start_time = time.time()
            await message.download(file_name=save_path, progress=progress_func, progress_args=(start_time, status_msg))
            
            await status_msg.edit("📤 **Uploading to Drive...**")
            
            file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
            media = MediaIoBaseUpload(open(save_path, 'rb'), mimetype=mime_type, resumable=True)
            
            file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webContentLink').execute()
            
            os.remove(save_path)
            
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete from Drive", callback_data=f"del_{file.get('id')}") ]])
            await status_msg.edit(f"✅ **Done!**\n📂 `{file_name}`\n🔗 [Download Link]({file.get('webContentLink')})", reply_markup=keyboard)
            
        except Exception as e:
            full_error = traceback.format_exc()
            print(f"ERROR: {full_error}")
            await status_msg.edit(f"❌ **Error:** {e}")
            if os.path.exists(save_path): os.remove(save_path)

@app.on_callback_query(filters.regex(r"^del_"))
async def delete_callback(client, callback_query):
    file_id = callback_query.data.split("_")[1]
    try:
        drive_service.files().delete(fileId=file_id).execute()
        await callback_query.answer("Deleted!", show_alert=True)
        await callback_query.message.edit_text("🗑️ **Deleted.**")
    except Exception as e:
        await callback_query.answer(f"Error: {e}", show_alert=True)

# --- FLASK KEEP ALIVE ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Running"
def run_flask(): flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- START BOT ---
if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    print("Bot Started...")
    app.run() # Ye sabse LAST line honi chahiye
    
