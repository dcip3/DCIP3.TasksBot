import os
import json
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Telegram API token and base URL for Deadline API
TG_API_TOKEN = os.getenv("TG_API_TOKEN")
BASE_API_URL = "https://renderfarm.local:4434/api"

# Path to credentials.json (user login/password storage)
CREDENTIALS_FILE = Path("credentials.json")

# Dropbox OAuth2 constants
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
TEAM_MEMBER_ID = os.getenv("DROPBOX_TEAM_MEMBER_ID")
ROOT_NAMESPACE_ID = os.getenv("DROPBOX_ROOT_NAMESPACE_ID")
DROPBOX_ROOT_MARKER = os.getenv("DROPBOX_ROOT_MARKER", "Team Folder")

# Mapping for worker and job statuses
WORKER_STATUS_MAP = {
    0: "Unknown",
    1: "Rendering",
    2: "Idle",
    3: "Offline",
    4: "Stalled",
    8: "StartingJob"
}

JOB_STATUS_MAP = {
    0: "Unknown",
    1: "Active",
    2: "Suspended",
    3: "Completed",
    4: "Failed",
    6: "Pending"
}

def load_credentials():
    """Load stored user credentials (login, password, notifications flag) from credentials.json."""
    if CREDENTIALS_FILE.exists():
        with CREDENTIALS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_credentials(data):
    """Save user credentials back to credentials.json."""
    with CREDENTIALS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f)

# Initialize user_credentials dictionary
user_credentials = load_credentials()

def get_auth_credentials(user_id):
    """
    Retrieve stored login, password, and notifications_enabled flag for a given user_id.
    Returns (login, password, notifications_enabled) or (None, None, False) if missing.
    """
    creds = user_credentials.get(str(user_id))
    if not creds or len(creds) < 2:
        return None, None, False
    login, password = creds[0], creds[1]
    notifications_enabled = creds[2] if len(creds) > 2 else False
    return login, password, notifications_enabled

def get_main_keyboard():
    """
    Return the main ReplyKeyboardMarkup for the bot (buttons: Jobs, Realtime, Workers, Notifications, Exit, Clear).
    """
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    kb = [
        [KeyboardButton(text="Jobs"), KeyboardButton(text="Realtime"), KeyboardButton(text="Workers")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="🚪 Выход")],
        [KeyboardButton(text="🧹 Очистить")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)