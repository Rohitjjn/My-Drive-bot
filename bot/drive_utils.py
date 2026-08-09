import os
import json
import asyncio
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from bot.config import logger

tokens_map = {}

def load_tokens():
    global tokens_map
    tokens_map.clear()
    for i in range(1, 3):
        token_content = os.environ.get(f"TOKEN_{i}")
        if token_content:
            try:
                token_dict = json.loads(token_content)
                creds = Credentials.from_authorized_user_info(token_dict)
                service = build('drive', 'v3', credentials=creds)
                tokens_map[i] = {'service': service, 'id': i}
                logger.info(f"✅ Loaded Account {i}")
            except Exception as e:
                logger.error(f"❌ Error loading TOKEN_{i}: {e}")
    if not tokens_map:
        logger.warning("❌ KOI TOKEN NAHI MILA! Check Render Config.")

async def get_best_drive_service(allowed_account_ids, file_size_bytes):
    for acc_id in allowed_account_ids:
        if acc_id in tokens_map:
            account = tokens_map[acc_id]
            try:
                service = account['service']
                about = await asyncio.to_thread(service.about().get(fields="storageQuota, user").execute)
                quota = about.get('storageQuota', {})
                limit = int(quota.get('limit', 0))
                usage = int(quota.get('usage', 0))
                free_space = limit - usage

                if free_space > file_size_bytes:
                    email = about.get('user', {}).get('emailAddress', 'Unknown')
                    return service, email, free_space, acc_id
            except Exception as e:
                logger.error(f"Error checking drive {acc_id}: {e}")
                continue
    return None, None, 0, 0

def empty_drive_trash(acc_id):
    if acc_id in tokens_map:
        try:
            service = tokens_map[acc_id]['service']
            service.files().emptyTrash().execute()
            return True, "Trash emptied successfully."
        except Exception as e:
            return False, str(e)
    return False, "Account not found."
