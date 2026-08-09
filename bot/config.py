import os
import json
import logging

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- BOT CONFIG ---
API_ID = int(os.environ.get("API_ID", "0") or "0")
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))

# --- USER & FOLDER CONFIG ---
# Admin 1 Config
ADMIN_1_ID = int(os.environ.get("ADMIN_1_ID", "0") or "0")
FOLDER_ID_1 = os.environ.get("FOLDER_ID_1", "")

# Admin 2 Config
ADMIN_2_ID = int(os.environ.get("ADMIN_2_ID", "0") or "0")
FOLDER_ID_2 = os.environ.get("FOLDER_ID_2", "")

AUTH_USERS = [ADMIN_1_ID, ADMIN_2_ID]

def get_user_config(user_id):
    """Returns target folder and allowed account ID based on user_id"""
    if user_id == ADMIN_1_ID:
        return FOLDER_ID_1, [1]
    elif user_id == ADMIN_2_ID:
        return FOLDER_ID_2, [2]
    return None, []
