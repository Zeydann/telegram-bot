import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Direntori proyek — dipakai sebagai default untuk path log dan statistik
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/tiktok_bot")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 50 * 1024 * 1024))  # 50MB default
INSTAGRAM_COOKIEFILE = os.getenv("INSTAGRAM_COOKIEFILE", "")  # path to cookies.txt

# --- Admin / otorisasi -------------------------------------------------------
# ADMIN_ID tetap didukung (kompatibel mundur); ADMIN_IDS = daftar dipisah koma,
# mis. ADMIN_IDS=11111111,22222222. Dipakai untuk /restart dan /logs.
ADMIN_ID = os.getenv("ADMIN_ID")


def _parse_admin_ids(*values: str | None) -> frozenset[int]:
    """Gabungkan ID admin dari beberapa variabel env. Nilai tidak valid diabaikan."""
    ids: set[int] = set()
    for raw in values:
        for part in (raw or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                # Jangan cetak nilainya — bisa saja orang menempel kredensial
                # di variabel yang salah.
                logger.warning("ADMIN_IDS memuat nilai bukan angka; diabaikan")
    return frozenset(ids)


ADMIN_IDS = _parse_admin_ids(ADMIN_ID, os.getenv("ADMIN_IDS"))


def is_admin(user_id) -> bool:
    """True bila user_id termasuk daftar admin (identitas: ID Telegram, bukan username)."""
    if user_id is None:
        return False
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


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


# --- Helper pembaca env angka (aman terhadap nilai rusak) --------------------
def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Env %s bukan angka; memakai default", name)
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Env %s bukan angka; memakai default", name)
        return default


def _threshold_pair(warn_var: str, crit_var: str,
                    default_warn: int, default_crit: int) -> tuple[int, int]:
    """Ambang batas persen (WARNING < CRITICAL), selalu dalam rentang valid."""
    warn = min(max(_int_env(warn_var, default_warn), 1), 99)
    crit = min(max(_int_env(crit_var, default_crit), warn + 1), 100)
    return warn, crit


# --- Server monitor (/status dan /health) -----------------------------------
# Ambang batas persen pemakaian resource; status NORMAL < WARNING <= CRITICAL.
CPU_WARNING, CPU_CRITICAL = _threshold_pair("CPU_WARNING", "CPU_CRITICAL", 70, 90)
RAM_WARNING, RAM_CRITICAL = _threshold_pair("RAM_WARNING", "RAM_CRITICAL", 80, 90)
DISK_WARNING, DISK_CRITICAL = _threshold_pair("DISK_WARNING", "DISK_CRITICAL", 80, 90)

# Ruang disk minimum (MB) agar operasi unduhan/konversi tetap aman
MIN_FREE_DISK_MB = _int_env("MIN_FREE_DISK_MB", 500)
# Batas waktu (detik) tiap pemeriksaan koneksi eksternal di /health
HEALTH_TIMEOUT = _float_env("HEALTH_TIMEOUT", 5)

# --- Log (/logs) -------------------------------------------------------------
# File log rotating yang dibaca command /logs (output jurnal systemd tetap ada).
LOG_FILE = os.getenv("LOG_FILE") or os.path.join(BASE_DIR, "logs", "bot.log")
LOG_MAX_BYTES = _int_env("LOG_MAX_BYTES", 1024 * 1024)
LOG_BACKUP_COUNT = _int_env("LOG_BACKUP_COUNT", 3)

# --- Statistik (/stats) ------------------------------------------------------
# File JSON persisten; statistik bertahan setelah bot direstart.
STATS_FILE = os.getenv("STATS_FILE") or os.path.join(BASE_DIR, "data", "stats.json")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Please configure it in .env")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
