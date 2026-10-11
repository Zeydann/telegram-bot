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

# --- Konversi dokumen (/pdf dan /word) ---
# Kunci API iLovePDF: https://developer.ilovepdf.com/user/projects
ILOVEPDF_PUBLIC_KEY = os.getenv("ILOVEPDF_PUBLIC_KEY", "")
ILOVEPDF_SECRET_KEY = os.getenv("ILOVEPDF_SECRET_KEY", "")
ILOVEPDF_REGION = os.getenv("ILOVEPDF_REGION", "")  # opsional: eu/us/fr/de/pl/in/sg

# Batas unduhan dokumen: getFile Telegram maksimal 20MB
CONVERT_MAX_FILE_SIZE = int(os.getenv("CONVERT_MAX_FILE_SIZE", 20 * 1024 * 1024))
CONVERT_TIMEOUT = int(os.getenv("CONVERT_TIMEOUT", 120))  # detik per request konversi
CONVERT_MAX_CONCURRENT = int(os.getenv("CONVERT_MAX_CONCURRENT", 2))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Please configure it in .env")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
