import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/tiktok_bot")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 50 * 1024 * 1024))  # 50MB default
ADMIN_ID = os.getenv("ADMIN_ID")
INSTAGRAM_COOKIEFILE = os.getenv("INSTAGRAM_COOKIEFILE", "")  # path to cookies.txt

RATE_LIMIT_COUNT = int(os.getenv("RATE_LIMIT_COUNT", 5))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", 60))  # seconds

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Please configure it in .env")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
