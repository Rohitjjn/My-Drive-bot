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

# --- 1. LOGGING & CONFIG ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# --- USER & FOLDER CONFIG ---
# Admin 1 Config
ADMIN_1_ID = int(os.environ.get("ADMIN_1_ID"))
FOLDER_ID_1 = os.environ.get("FOLDER_ID_1") 

# Admin 2 Config
ADMIN_2_ID = int(os.environ.get("ADMIN_2_ID"))
FOLDER_ID_2 = os.environ.get("FOLDER_ID_2") 

AUTH_USERS = [ADMIN_1_ID, ADMIN_2_ID]

# --- 2. MULTI-ACCOUNT TOKEN LOADER ---
tokens_map = {}
for i in range(1, 3):
    token_content = os.environ.get(f"TOKEN_{i}")
    if token_content:
        try:
            token_dict = json.loads(token_content)
            creds = Credentials.from_authorized_user_info(token_dict)
            service = build('drive', 'v3', credentials=creds)
            tokens_map[i] = {'service': service, 'id': i}
            print(f"✅ Loaded Account {i}")
        except Exception as e:
            print(f"❌ Error loading TOKEN_{i}: {e}")

if not tokens_map:
    print("❌ KOI TOKEN NAHI MILA! Check Render Config.")

# --- 3. CONFIG LOGIC ---
def get_user_config(user_id):
    if user_id == ADMIN_1_ID:
        return FOLDER_ID_1, [1] # User 1 -> Folder 1 -> Account 1
    elif user_id == ADMIN_2_ID:
        return FOLDER_ID_2, [2] # User 2 -> Folder 2 -> Account 2
    return None, []

# --- 4. DRIVE SERVICE SELECTOR ---
def get_best_drive_service(allowed_account_ids, file_size_bytes):
    for acc_id in allowed_account_ids:
        if acc_id in tokens_map:
            account = tokens_map[acc_id]
            try:
                service = account['service']
                about = service.about().get(fields="storageQuota, user").execute()
                quota = about.get('storageQuota', {})
                limit = int(quota.get('limit', 0))
                usage = int(quota.get('usage', 0))
                free_space = limit - usage
                
                if free_space > file_size_bytes:
                    email = about.get('user', {}).get('emailAddress', 'Unknown')
                    return service, email, free_space, acc_id
            except: continue
    return None, None, 0, 0

# --- 5. BOT SETUP ---
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
upload_semaphore = asyncio.Semaphore(4)

# --- HELPERS (Formatting) ---
def humanbytes(size):
    if not size: return "0B"
    power = 2**10
    n = 0
    dic_powerN = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + "" + dic_powerN[n] + 'B'

def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
          ((str(hours) + "h, ") if hours else "") + \
          ((str(minutes) + "m, ") if minutes else "") + \
          ((str(seconds) + "s") if seconds else "")
    return tmp[:-2] if tmp.endswith(", ") else tmp

# --- NEW PROGRESS BAR FUNCTION ---
async def progress_func(current, total, start_time, status_msg, file_name):
    now = time.time()
    diff = now - start_time
    
    # Har 5 second me update karenge taaki bot block na ho
    if round(diff % 5.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000
        estimated_total_time = elapsed_time + time_to_completion
        
        # Visual Bar [■■■□□□]
        filled_length = int(10 * current // total)
        bar = '■' * filled_length + '□' * (10 - filled_length)
        
        # Message Format as requested
        text = f"📂 **File Name:** `{file_name}`\n\n"
        text += f"[{bar}] {round(percentage, 2)}%\n\n"
        text += f"💾 **Size:** {humanbytes(current)} / {humanbytes(total)}\n"
        text += f"🚀 **Speed:** {humanbytes(speed)}/sec\n"
        text += f"⏳ **EST Time:** {time_formatter(time_to_completion)}"
        
        try:
            await status_msg.edit(text)
        except:
            pass

# --- STATS COMMAND (UPDATED WITH FILE COUNT) ---
@app.on_message(filters.command("stats") & filters.user(AUTH_USERS))
async def stats_handler(client, message):
    user_id = message.from_user.id
    target_folder, allowed_ids = get_user_config(user_id)
    
    if not target_folder:
        await message.reply_text("❌ Config Error.")
        return

    status_msg = await message.reply_text("📊 **Calculating Stats & Files...**")
    
    # Identify User Name
    user_name = "User 1" if user_id == ADMIN_1_ID else "User 2"
    
    # Data fetch logic
    acc_id = allowed_ids[0] # Humare case me har user ka 1 hi account hai
    
    if acc_id in tokens_map:
        account = tokens_map[acc_id]
        try:
            service = account['service']
            
            # 1. Storage Quota
            about = service.about().get(fields="storageQuota").execute()
            quota = about.get('storageQuota', {})
            limit = int(quota.get('limit', 0))
            usage = int(quota.get('usage', 0))
            free = limit - usage
            
            used_percent = (usage / limit) * 100 if limit > 0 else 0
            free_percent = 100 - used_percent
            
            # 2. File Count Logic (Loop to count files in folder)
            file_count = 0
            page_token = None
            query = f"'{target_folder}' in parents and trashed = false"
            
            while True:
                response = service.files().list(
                    q=query,
                    spaces='drive',
                    fields='nextPageToken, files(id)',
                    pageToken=page_token
                ).execute()
                
                files = response.get('files', [])
                file_count += len(files)
                
                page_token = response.get('nextPageToken')
                if not page_token:
                    break
            
            # 3. Final Output Format
            stats_text = (
                f"📊 👤 **Stats for {user_name}:**\n\n"
                f"💿 **Total:** {humanbytes(limit)}\n"
                f"📦 **Used:** {humanbytes(usage)} ({round(used_percent, 2)}%)\n"
                f"📦 **Free:** {humanbytes(free)} ({round(free_percent, 2)}%)\n"
                f"✅ **Total Files in Drive:** {file_count}"
            )
            
            await status_msg.edit(stats_text)
            
        except Exception as e:
            await status_msg.edit(f"❌ Error fetching stats: {e}")
    else:
        await status_msg.edit("❌ Account not connected.")


# --- UPLOAD HANDLER (UPDATED PROGRESS) ---
@app.on_message(filters.user(AUTH_USERS) & (filters.document | filters.video))
async def upload_handler(client, message):
    status_msg = await message.reply_text("⏳ **Preparing...**")
    
    file_size = message.video.file_size if message.video else message.document.file_size
    file_name = message.video.file_name if message.video else message.document.file_name
    if not file_name: file_name = "video.mp4"
    
    user_id = message.from_user.id
    target_folder_id, allowed_accounts = get_user_config(user_id)
    
    if not target_folder_id:
        await status_msg.edit("❌ Config Error.")
        return

    best_service, email_used, free_space, acc_id = get_best_drive_service(allowed_accounts, file_size)
    
    if not best_service:
        await status_msg.edit(f"❌ **Storage Full!**")
        return
        
    async with upload_semaphore:
        # Initial Message
        await status_msg.edit(f"🚀 **Starting Upload...**\nAccount: {email_used}")
        
        save_path = f"downloads/{file_name}"
        if not os.path.exists("downloads"): os.makedirs("downloads")

        try:
            start_time = time.time()
            # Pass file_name to progress_func
            await message.download(
                save_path, 
                progress=progress_func, 
                progress_args=(start_time, status_msg, file_name)
            )
            
            await status_msg.edit(f"📤 **Finalizing Upload to Drive...**")
            
            file_metadata = {'name': file_name, 'parents': [target_folder_id]}
            media = MediaIoBaseUpload(open(save_path, 'rb'), mimetype=message.video.mime_type if message.video else message.document.mime_type, resumable=True)
            
            file = best_service.files().create(body=file_metadata, media_body=media, fields='id, webContentLink').execute()
            
            if os.path.exists(save_path): os.remove(save_path)
            
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"📂 `{file_name}`\n"
                f"🔗 [Download Link]({file.get('webContentLink')})", 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Delete from Drive", callback_data=f"del_{file.get('id')}")]]))
            
        except Exception as e:
            await status_msg.edit(f"❌ Error: {e}")
            if os.path.exists(save_path): os.remove(save_path)

# --- DELETE HANDLER ---
@app.on_callback_query(filters.regex(r"^del_"))
async def delete_callback(client, callback_query):
    file_id = callback_query.data[4:]
    user_id = callback_query.from_user.id
    
    if user_id not in AUTH_USERS:
        await callback_query.answer("❌ Not Authorized.", show_alert=True)
        return

    target_folder, allowed_accounts = get_user_config(user_id)
    file_deleted = False

    try:
        for acc_id in allowed_accounts:
            if acc_id in tokens_map:
                try:
                    service = tokens_map[acc_id]['service']
                    service.files().delete(fileId=file_id).execute()
                    file_deleted = True
                    break
                except: continue
            
        if file_deleted:
            await callback_query.answer("🗑️ Deleted!", show_alert=True)
            await callback_query.message.delete()
        else:
             await callback_query.answer("⚠️ Already Deleted.", show_alert=True)
             await callback_query.message.delete()
            
    except Exception as e:
        await callback_query.answer(f"❌ Error: {str(e)}", show_alert=True)

# --- SERVER ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot Running!"
def run_flask(): flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    app.run()
    
