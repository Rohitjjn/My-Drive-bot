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
# Hum sirf 2 accounts load karenge: 1 aur 2
tokens_map = {}

# Range (1, 3) matlab 1 aur 2
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

# --- 3. CORE LOGIC: USER CONFIGURATION ---
def get_user_config(user_id):
    """
    User 1 -> Account 1 -> Folder 1
    User 2 -> Account 2 -> Folder 2
    """
    if user_id == ADMIN_1_ID:
        # User 1 sirf Account [1] use karega
        return FOLDER_ID_1, [1]
    
    elif user_id == ADMIN_2_ID:
        # User 2 sirf Account [2] use karega
        return FOLDER_ID_2, [2]
        
    return None, []

# --- 4. SMART DRIVE SELECTOR ---
def get_best_drive_service(allowed_account_ids, file_size_bytes):
    # Sirf user ke allowed account (ek hi hai) ko check karega
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
                
                # Agar space hai to return karo
                if free_space > file_size_bytes:
                    email = about.get('user', {}).get('emailAddress', 'Unknown')
                    return service, email, free_space, acc_id
            except Exception as e:
                print(f"Error checking Acc {acc_id}: {e}")
                continue
            
    return None, None, 0, 0

# --- 5. BOT SETUP ---
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

async def progress_func(current, total, start_time, status_msg):
    now = time.time()
    diff = now - start_time
    if round(diff % 5.00) == 0 or current == total:
        speed = current / diff
        percentage = current * 100 / total
        await status_msg.edit(f"📥 **Downloading...** {round(percentage)}%\nSpeed: {humanbytes(speed)}/s")

# --- STATS COMMAND ---
@app.on_message(filters.command("stats") & filters.user(AUTH_USERS))
async def stats_handler(client, message):
    user_id = message.from_user.id
    target_folder, allowed_ids = get_user_config(user_id)
    
    if not target_folder:
        await message.reply_text("❌ Aap authorized user nahi hain.")
        return

    status_msg = await message.reply_text("📊 **Scanning Your Storage...**")
    
    admin_name = "User 1" if user_id == ADMIN_1_ID else "User 2"
    stats_text = f"👤 **Stats for {admin_name}**\n📂 Folder ID: `{target_folder}`\n\n"
    
    total_capacity = 0
    total_used = 0
    
    for acc_id in allowed_ids:
        if acc_id in tokens_map:
            account = tokens_map[acc_id]
            try:
                service = account['service']
                quota = service.about().get(fields="storageQuota").execute().get('storageQuota', {})
                limit = int(quota.get('limit', 0))
                usage = int(quota.get('usage', 0))
                free = limit - usage
                
                total_capacity += limit
                total_used += usage
                
                stats_text += f"💿 **Acc {acc_id}:** Free {humanbytes(free)} / {humanbytes(limit)}\n"
            except:
                stats_text += f"❌ Acc {acc_id}: Error\n"

    stats_text += "------------------\n"
    stats_text += f"📦 **TOTAL:** {humanbytes(total_used)} used of {humanbytes(total_capacity)}"
    
    await status_msg.edit(stats_text)

# --- UPLOAD HANDLER ---
@app.on_message(filters.user(AUTH_USERS) & (filters.document | filters.video))
async def upload_handler(client, message):
    status_msg = await message.reply_text("⏳ **Checking Your Quota...**")
    
    file_size = message.video.file_size if message.video else message.document.file_size
    file_name = message.video.file_name if message.video else message.document.file_name
    if not file_name: file_name = "video.mp4"
    
    user_id = message.from_user.id
    
    # 1. Config Load karo
    target_folder_id, allowed_accounts = get_user_config(user_id)
    
    if not target_folder_id:
        await status_msg.edit("❌ Config Error.")
        return

    # 2. Check Space (Sirf user ke ek account me)
    best_service, email_used, free_space, acc_id = get_best_drive_service(allowed_accounts, file_size)
    
    if not best_service:
        await status_msg.edit(f"❌ **Storage Full!**\nAapka assigned account (Acc {allowed_accounts[0]}) full hai.")
        return
        
    async with upload_semaphore:
        await status_msg.edit(f"🚀 **Using Account {acc_id}**\n(Free: {humanbytes(free_space)})\n\n📥 **Downloading...**")
        
        save_path = f"downloads/{file_name}"
        if not os.path.exists("downloads"): os.makedirs("downloads")

        try:
            start = time.time()
            await message.download(save_path, progress=progress_func, progress_args=(start, status_msg))
            
            await status_msg.edit(f"📤 **Uploading to Your Folder...**\n(Account: {email_used})")
            
            # 3. Upload to User's specific Folder
            file_metadata = {'name': file_name, 'parents': [target_folder_id]}
            media = MediaIoBaseUpload(open(save_path, 'rb'), mimetype=message.video.mime_type if message.video else message.document.mime_type, resumable=True)
            
            file = best_service.files().create(body=file_metadata, media_body=media, fields='id, webContentLink').execute()
            
            if os.path.exists(save_path): os.remove(save_path)
            
            await status_msg.edit(f"✅ **Upload Successful!**\n\n📂 `{file_name}`\n🔗 [Download Link]({file.get('webContentLink')})", 
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Delete from Drive", callback_data=f"del_{file.get('id')}")]]))
            
        except Exception as e:
            error_str = str(e)
            if "File not found" in error_str and "parents" in error_str:
                 await status_msg.edit(f"❌ **Permission Error:**\nAccount `{email_used}` folder `{target_folder_id}` ko access nahi kar pa raha.\n\n👉 Ensure Folder ID sahi hai aur Account ke paas permission hai.")
            else:
                await status_msg.edit(f"❌ Error: {e}")
            if os.path.exists(save_path): os.remove(save_path)

# --- DELETE BUTTON HANDLER ---
@app.on_callback_query(filters.regex(r"^del_"))
async def delete_callback(client, callback_query):
    file_id = callback_query.data[4:]
    user_id = callback_query.from_user.id
    
    if user_id not in AUTH_USERS:
        await callback_query.answer("❌ You are not authorized.", show_alert=True)
        return

    # Delete karne ke liye user ke account try karenge
    target_folder, allowed_accounts = get_user_config(user_id)
    file_deleted = False

    try:
        # User ke assigned account se delete karne ki koshish
        for acc_id in allowed_accounts:
            if acc_id in tokens_map:
                try:
                    service = tokens_map[acc_id]['service']
                    service.files().delete(fileId=file_id).execute()
                    file_deleted = True
                    break
                except:
                    continue
            
        if file_deleted:
            await callback_query.answer("🗑️ Deleted!", show_alert=True)
            await callback_query.message.delete()
        else:
             await callback_query.answer("⚠️ Already Deleted or Not Found in your account.", show_alert=True)
             await callback_query.message.delete()
            
    except Exception as e:
        await callback_query.answer(f"❌ Error: {str(e)}", show_alert=True)

# --- FAKE SERVER ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "2-User Bot Running!"
def run_flask(): flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    app.run()
                
