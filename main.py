import os
import time
import asyncio
import threading
import shutil
import uuid
import traceback

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask

from bot.config import API_ID, API_HASH, BOT_TOKEN, PORT, AUTH_USERS, get_user_config, logger
from bot.helpers import humanbytes, time_formatter, get_sysinfo, create_progress_text
from bot.drive_utils import load_tokens, get_best_drive_service, tokens_map, empty_drive_trash
from googleapiclient.http import MediaIoBaseUpload

# Setup a new event loop before importing pyrogram
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
_upload_semaphore = None

# --- TASK REGISTRY FOR CANCELLATION ---
# active_tasks[task_id] = {"cancel": False}
active_tasks = {}

def get_upload_semaphore():
    global _upload_semaphore
    if _upload_semaphore is None:
        _upload_semaphore = asyncio.Semaphore(4)
    return _upload_semaphore

# --- START COMMAND ---
@app.on_message(filters.command("start") & filters.user(AUTH_USERS))
async def start_handler(client, message):
    welcome_text = (
        "👋 **Welcome to the Google Drive Uploader Bot!**\n\n"
        "I can help you upload files directly to your Google Drive.\n"
        "Use /help to see what I can do."
    )
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Help", callback_data="btn_help"), InlineKeyboardButton("Drives", callback_data="btn_drives")],
        [InlineKeyboardButton("SysInfo", callback_data="btn_sysinfo")]
    ])
    await message.reply_text(welcome_text, reply_markup=buttons)

# --- HELP COMMAND ---
async def send_help_text(message):
    help_text = (
        "🛠 **How to use me:**\n\n"
        "1. Just send me any file or video.\n"
        "2. I will automatically upload it to your designated Google Drive folder.\n\n"
        "**Available Commands:**\n"
        "🔹 /start - Start the bot\n"
        "🔹 /help - Show this help message\n"
        "🔹 /ping - Check bot latency\n"
        "🔹 /sysinfo - Check Render Server Stats\n"
        "🔹 /drives - Check Drive storage status\n"
        "🔹 /files - List all your uploaded files\n"
        "🔹 /clear - Clear temporary local downloads\n"
    )
    await message.reply_text(help_text)

@app.on_message(filters.command("help") & filters.user(AUTH_USERS))
async def help_handler(client, message):
    await send_help_text(message)

# --- PING COMMAND ---
@app.on_message(filters.command("ping") & filters.user(AUTH_USERS))
async def ping_handler(client, message):
    start_time = time.time()
    ping_msg = await message.reply_text("🏓 **Pong!**")
    end_time = time.time()
    latency = round((end_time - start_time) * 1000, 2)
    await ping_msg.edit(f"🏓 **Pong!**\nLatency: `{latency} ms`")

# --- SYSINFO COMMAND ---
@app.on_message(filters.command("sysinfo") & filters.user(AUTH_USERS))
async def sysinfo_handler(client, message):
    info = await asyncio.to_thread(get_sysinfo)
    await message.reply_text(info)

# --- CLEAR COMMAND ---
@app.on_message(filters.command("clear") & filters.user(AUTH_USERS))
async def clear_handler(client, message):
    download_dir = "downloads"
    cleared_space = 0
    if os.path.exists(download_dir):
        for filename in os.listdir(download_dir):
            filepath = os.path.join(download_dir, filename)
            try:
                if os.path.isfile(filepath):
                    cleared_space += os.path.getsize(filepath)
                    os.remove(filepath)
            except Exception as e:
                logger.error(f"Failed to delete {filepath}: {e}")
    await message.reply_text(f"🧹 **Cleared Temporary Files!**\nFree up: `{humanbytes(cleared_space)}`")

# --- DRIVES COMMAND ---
async def send_drives_info(message):
    status_msg = await message.reply_text("📊 **Fetching Drives Info...**")
    text = "📊 **Google Drives Status:**\n\n"
    buttons = []
    
    for acc_id, account in tokens_map.items():
        try:
            service = account['service']
            about = await asyncio.to_thread(service.about().get(fields="storageQuota, user").execute)
            quota = about.get('storageQuota', {})
            limit = int(quota.get('limit', 0))
            usage = int(quota.get('usage', 0))
            email = about.get('user', {}).get('emailAddress', 'Unknown')

            free = limit - usage
            used_percent = (usage / limit) * 100 if limit > 0 else 0
            
            text += (
                f"👤 **Account {acc_id}:** `{email}`\n"
                f"💿 **Total:** `{humanbytes(limit)}`\n"
                f"📦 **Used:** `{humanbytes(usage)}` ({round(used_percent, 2)}%)\n"
                f"✅ **Free:** `{humanbytes(free)}`\n\n"
            )
            buttons.append([InlineKeyboardButton(f"🗑 Empty Trash Acc {acc_id}", callback_data=f"empty_trash_{acc_id}")])
        except Exception as e:
            text += f"👤 **Account {acc_id}:** ❌ Error fetching stats.\n\n"

    if not tokens_map:
        text = "❌ No Drive accounts connected."

    await status_msg.edit(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

@app.on_message(filters.command("drives") & filters.user(AUTH_USERS))
async def drives_handler(client, message):
    await send_drives_info(message)

# --- FILES COMMAND ---
@app.on_message(filters.command("files") & filters.user(AUTH_USERS))
async def list_files_handler(client, message):
    user_id = message.from_user.id
    target_folder, allowed_ids = get_user_config(user_id)
    
    if not target_folder:
        await message.reply_text("❌ Config Error.")
        return

    status_msg = await message.reply_text("🔍 **Fetching File List...**\n(Please wait)")
    
    acc_id = allowed_ids[0]
    if acc_id in tokens_map:
        account = tokens_map[acc_id]
        try:
            service = account['service']
            files_found = []
            page_token = None
            
            query = f"'{target_folder}' in parents and trashed = false"
            while True:
                response = await asyncio.to_thread(service.files().list(
                    q=query, spaces='drive', fields='nextPageToken, files(id, name, size, webContentLink)', pageToken=page_token
                ).execute)
                files_found.extend(response.get('files', []))
                page_token = response.get('nextPageToken')
                if not page_token: break
            
            if not files_found:
                await status_msg.edit("📂 **Folder is Empty!**")
                return

            await status_msg.edit(f"✅ Found {len(files_found)} files. Listing below...")
            
            for file in files_found:
                f_name = file.get('name', 'Unknown')
                f_id = file.get('id')
                f_link = file.get('webContentLink', '#')
                f_size = int(file.get('size', 0))
                
                text = (
                    f"📂 `{f_name}`\n"
                    f"💾 **File Size:** {humanbytes(f_size)}\n"
                    f"🔗 [Download Link]({f_link})"
                )
                
                await message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Delete from Drive", callback_data=f"del_{f_id}")]])
                )
                await asyncio.sleep(0.5)

        except Exception as e:
            await message.reply_text(f"❌ Error listing files: {e}")
    else:
        await status_msg.edit("❌ Account not connected.")

# --- UPLOAD HANDLER WITH PROGRESS AND CANCEL ---
async def progress_for_pyrogram(current, total, start_time, status_msg, file_name, task_id, action="Downloading"):
    # Check if cancelled
    if active_tasks.get(task_id, {}).get("cancel", False):
        await status_msg.client.stop_transmission()
        return

    now = time.time()
    diff = now - start_time

    # Update every 5 seconds or when done
    if round(diff % 5.00) == 0 or current == total:
        text = create_progress_text(action, current, total, start_time, file_name)
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]])
        try:
            await status_msg.edit(text, reply_markup=buttons)
        except Exception:
            pass

@app.on_message(filters.user(AUTH_USERS) & (filters.document | filters.video))
async def upload_handler(client, message):
    task_id = str(uuid.uuid4())
    active_tasks[task_id] = {"cancel": False}

    status_msg = await message.reply_text("⏳ **Preparing...**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]]))
    
    file_size = message.video.file_size if message.video else message.document.file_size
    file_name = message.video.file_name if message.video else message.document.file_name
    if not file_name: file_name = "video.mp4"
    
    user_id = message.from_user.id
    target_folder_id, allowed_accounts = get_user_config(user_id)
    
    if not target_folder_id:
        await status_msg.edit("❌ Config Error.")
        return

    best_service, email_used, free_space, acc_id = await get_best_drive_service(allowed_accounts, file_size)
    
    if not best_service:
        await status_msg.edit("❌ **Storage Full!**")
        return
        
    async with get_upload_semaphore():
        save_path = f"downloads/{task_id}_{file_name}"
        if not os.path.exists("downloads"): os.makedirs("downloads")

        try:
            # 1. DOWNLOAD FROM TELEGRAM
            start_time = time.time()
            downloaded_file = await message.download(
                save_path,
                progress=progress_for_pyrogram,
                progress_args=(start_time, status_msg, file_name, task_id, "Downloading to Server")
            )
            
            if active_tasks.get(task_id, {}).get("cancel", False) or not downloaded_file:
                raise Exception("Task Cancelled")

            # 2. UPLOAD TO DRIVE
            start_time = time.time()
            file_metadata = {'name': file_name, 'parents': [target_folder_id]}
            media = MediaIoBaseUpload(open(save_path, 'rb'), mimetype=message.video.mime_type if message.video else message.document.mime_type, resumable=True, chunksize=1024*1024*5) # 5MB Chunks
            
            request = best_service.files().create(body=file_metadata, media_body=media, fields='id, webContentLink, size')
            
            response = None
            uploaded_bytes = 0
            total_size = os.path.getsize(save_path)

            while response is None:
                if active_tasks.get(task_id, {}).get("cancel", False):
                    raise Exception("Task Cancelled")

                status, response = await asyncio.to_thread(request.next_chunk)
                if status:
                    uploaded_bytes = int(status.resumable_progress)
                    # Update upload progress
                    now = time.time()
                    diff = now - start_time
                    if round(diff % 5.00) == 0:
                        text = create_progress_text("Uploading to Drive", uploaded_bytes, total_size, start_time, file_name)
                        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]])
                        try:
                            await status_msg.edit(text, reply_markup=buttons)
                        except: pass
            
            file = response
            
            # --- SUCCESS MESSAGE ---
            final_size = int(file.get('size', file_size))
            await status_msg.edit(
                f"✅ **Upload Complete!**\n\n"
                f"📂 `{file_name}`\n"
                f"💾 **File Size:** `{humanbytes(final_size)}`\n"
                f"🔗 [Download Link]({file.get('webContentLink')})", 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Delete from Drive", callback_data=f"del_{file.get('id')}")]])
            )
            
        except Exception as e:
            if str(e) == "Task Cancelled":
                await status_msg.edit("❌ **Task Cancelled by User.**")
            else:
                await status_msg.edit(f"❌ **Error:** {e}")
                logger.error(f"Upload error: {traceback.format_exc()}")
        finally:
            # Clean up
            if os.path.exists(save_path): os.remove(save_path)
            if task_id in active_tasks:
                del active_tasks[task_id]

# --- CALLBACK QUERY HANDLERS ---
@app.on_callback_query(filters.regex(r"^btn_help$"))
async def btn_help_callback(client, callback_query):
    await send_help_text(callback_query.message)
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^btn_drives$"))
async def btn_drives_callback(client, callback_query):
    await send_drives_info(callback_query.message)
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^btn_sysinfo$"))
async def btn_sysinfo_callback(client, callback_query):
    info = await asyncio.to_thread(get_sysinfo)
    await callback_query.message.edit(info)
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^cancel_"))
async def cancel_callback(client, callback_query):
    task_id = callback_query.data.split("_")[1]
    if task_id in active_tasks:
        active_tasks[task_id]["cancel"] = True
        await callback_query.answer("⚠️ Cancelling task...", show_alert=True)
    else:
        await callback_query.answer("❌ Task not found or already completed.", show_alert=True)

@app.on_callback_query(filters.regex(r"^empty_trash_"))
async def empty_trash_callback(client, callback_query):
    acc_id = int(callback_query.data.split("_")[2])
    await callback_query.answer("🗑 Emptying trash...", show_alert=False)
    success, msg = empty_drive_trash(acc_id)
    if success:
        await callback_query.answer("✅ Trash Emptied!", show_alert=True)
        await send_drives_info(callback_query.message) # Refresh UI
    else:
        await callback_query.answer(f"❌ Error: {msg}", show_alert=True)

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
                    await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
                    file_deleted = True
                    break
                except: continue
            
        if file_deleted:
            await callback_query.answer("🗑️ Deleted!", show_alert=True)
            await callback_query.message.delete()
        else:
             await callback_query.answer("⚠️ Already Deleted or not found.", show_alert=True)
             await callback_query.message.delete()
            
    except Exception as e:
        await callback_query.answer(f"❌ Error: {str(e)}", show_alert=True)

# --- SERVER ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot Running!"
def run_flask(): flask_app.run(host="0.0.0.0", port=PORT)

def main():
    load_tokens()
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    logger.info("Bot starting...")
    app.run()

if __name__ == "__main__":
    main()
