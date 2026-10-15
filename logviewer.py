"""File logging dan command /logs (khusus admin).

Pendukung fitur Server Monitor:

* :data:`LOG_FORMAT` — format log tunggal dipakai ``basicConfig`` di ``bot.py``
  dan handler file, sehingga /logs membaca format yang memang dipakai bot.
* :func:`setup_file_logging` — menambahkan RotatingFileHandler (izin 0600)
  ke logger root. Output jurnal systemd tetap berjalan seperti biasa; file ini
  hanya sumber bacaan untuk /logs.
* :func:`redact` — menyensor token/kredensial dari setiap baris sebelum
  dikirim ke Telegram.
* :func:`tail_lines` — membaca N baris terakhir dari **akhir** file per-chunk,
  jadi file besar tidak pernah dimuat utuh ke memori.
* :func:`logs_cmd` — handler /logs: otorisasi admin dijalankan **sebelum**
  file log disentuh, lalu output dikirim sebagai pesan atau file .txt.

Project ini tidak menyediakan command eksekusi perintah server apa pun.
"""

import asyncio
import html as html_lib
import io
import logging
import logging.handlers
import os
import re
import time

from config import (
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_MAX_BYTES,
    is_admin,
)
from utils import WIB, now_wib

logger = logging.getLogger(__name__)

# Format log — satu sumber kebenaran untuk journald/stdout dan file.
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# asctime dalam LOG_FORMAT harus selalu WIB, bukan timezone sistem
# (server produksi berjalan dalam UTC). gmtime(+7 jam) menghasilkan struct_time
# yang membaca jam dinding WIB; berlaku untuk semua Formatter, termasuk
# basicConfig() di bot.py yang dibuat setelah modul ini diimpor.
_WIB_OFFSET = WIB.utcoffset(None).total_seconds()


class _WibConverter:
    """Pengganti ``logging.Formatter.converter`` — epoch → jam dinding WIB.

    Dipakai sebagai objek (bukan fungsi) supaya tidak jadi bound method saat
    di-assign sebagai atribut kelas Formatter.
    """

    def __call__(self, timestamp: float):
        return time.gmtime(timestamp + _WIB_OFFSET)


logging.Formatter.converter = _WibConverter()

DEFAULT_LINES = 50      # baris yang dikirim bila user tidak menyebut jumlah
MAX_LINES = 200         # batas keras per permintaan
_CHUNK = 64 * 1024      # ukuran baca mundur per langkah
_SCAN_LIMIT = 512 * 1024  # batas total yang dibaca mundur (file besar tetap murah)
_MESSAGE_LIMIT = 3900   # di bawah limit pesan Telegram 4096 (sisa untuk escaping)

_LEVEL_ALIASES = {"WARN": "WARNING"}
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

USAGE_TEXT = (
    "Pakai: /logs [jumlah] [level]\n"
    "Contoh: /logs 50, /logs 30 ERROR, /logs WARNING\n"
    f"Jumlah baris: 1–{MAX_LINES}; level: INFO, WARNING, ERROR, CRITICAL."
)

# --- Sanitasi rahasia --------------------------------------------------------
# Urutan penting: nilai di balik nama variabel rahasia diredaksi lebih dulu,
# lalu token yang muncul bebas di teks.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:token|secret|passwd|password|api_?key|"
    r"private_?key|credential|authorization|cookie)[A-Za-z0-9_]*)\s*[=:]\s*"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)
_TG_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Ganti token/kredensial apa pun di `text` dengan [REDACTED].

    Berlaku untuk token bot Telegram, nilai env bergaya KEY=..., JWT iLovePDF,
    dan header Authorization. Lebih baik terlalu banyak daripada bocor.
    """
    # Bearer/Basic dulu: kalau kebalik, "authorization: Bearer X" akan memotong
    # hanya kata "Bearer" dan nilai X tertinggal.
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    text = _TG_TOKEN.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    return text


# --- File logging ---------------------------------------------------------------

class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler yang selalu menjaga izin file log 0600."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass  # izin longgar bukan alasan logging gagal
        return stream


def setup_file_logging() -> bool:
    """Tambahkan handler file ke logger root untuk /logs. Aman dipanggil berulang."""
    root = logging.getLogger()
    absolute = os.path.abspath(LOG_FILE)
    for handler in root.handlers:
        if getattr(handler, "baseFilename", None) == absolute:
            return True  # sudah terpasang (main() bisa dipanggil lebih dari sekali)
    try:
        directory = os.path.dirname(absolute)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        handler = _PrivateRotatingFileHandler(
            absolute,
            maxBytes=max(1, LOG_MAX_BYTES),
            backupCount=max(0, LOG_BACKUP_COUNT),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        return True
    except (OSError, ValueError) as exc:
        root.warning("File log tidak dapat dibuka (%s); /logs tidak akan punya data",
                     type(exc).__name__)
        return False


# --- Membaca ekor file -------------------------------------------------------

def _extract_lines(chunks: list[bytes], reached_start: bool,
                   matcher: re.Pattern | None) -> list[str]:
    raw = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    lines = raw.split("\n")
    if not reached_start:
        lines = lines[1:]  # potongan pertama bisa setengah baris — buang
    result = []
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            continue
        if matcher is None or matcher.search(line):
            result.append(line)
    return result


def tail_lines(path: str, max_lines: int, level: str | None = None) -> list[str]:
    """Maksimal `max_lines` baris terakhir `path` (opsional filter level).

    Membaca mundur per-chunk dari akhir file dan berhenti begitu baris yang
    dibutuhkan terkumpul — file besar tidak pernah dibaca seluruhnya.
    """
    matcher = re.compile(r"\s-\s" + re.escape(level) + r"\s-") if level else None
    chunks: list[bytes] = []
    scanned = 0
    reached_start = False

    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        while pos > 0 and scanned < _SCAN_LIMIT:
            step = min(_CHUNK, pos)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step)
            if not chunk:
                break
            chunks.append(chunk)
            scanned += step
            if pos == 0:
                reached_start = True
                break
            if len(_extract_lines(chunks, False, matcher)) >= max_lines:
                break

    return _extract_lines(chunks, reached_start, matcher)[-max_lines:]


# --- Command /logs -------------------------------------------------------------

def _parse_args(args: list[str]) -> tuple[int, str | None, str | None]:
    """→ (jumlah_baris, level, pesan_error)."""
    count, level = DEFAULT_LINES, None
    for arg in args:
        upper = _LEVEL_ALIASES.get(arg.upper(), arg.upper())
        if upper in _LEVELS:
            level = upper
        elif arg.isdigit():
            count = int(arg)
        else:
            return DEFAULT_LINES, None, USAGE_TEXT
    return max(1, min(count, MAX_LINES)), level, None


def make_log_filename() -> str:
    """Nama file .txt untuk kiriman log — stempel waktu dalam WIB."""
    return f"bot-logs-{now_wib().strftime('%Y%m%d-%H%M%S')}.txt"


async def logs_cmd(update, context) -> None:
    """Ambil potongan log terbaru — khusus user yang terdaftar sebagai admin."""
    message = update.message
    user_id = getattr(update.effective_user, "id", None)

    # Otorisasi DILAKUKAN SEBELUM file log disentuh. Identitas = ID Telegram
    # dari config (ADMIN_ID / ADMIN_IDS), bukan username.
    if not is_admin(user_id):
        logger.warning("Akses /logs ditolak untuk user %s", user_id)
        await message.reply_text("Kamu tidak punya akses untuk perintah ini.")
        return

    count, level, usage_error = _parse_args(list(context.args or []))
    if usage_error:
        await message.reply_text(usage_error)
        return

    try:
        lines = await asyncio.to_thread(tail_lines, LOG_FILE, count, level)
    except FileNotFoundError:
        await message.reply_text(
            "File log belum ditemukan di server:\n"
            f"<code>{html_lib.escape(LOG_FILE)}</code>\n\n"
            "Log akan muncul setelah bot menulis log pertama. Sementara itu, "
            "log sistem tersedia lewat <code>journalctl -u telegram-bot</code>.",
            parse_mode="HTML",
        )
        return
    except OSError as exc:
        # Galat membaca file (izin/disk) — jangan tampilkan detail internal berlebihan.
        logger.warning("Gagal membaca file log: %s", type(exc).__name__)
        await message.reply_text(
            f"File log tidak dapat dibaca saat ini ({exc.strerror or type(exc).__name__})."
        )
        return

    if not lines:
        suffix = f" level {level}" if level else ""
        await message.reply_text(
            f"Tidak ada baris log{suffix} yang tercatat di "
            f"<code>{html_lib.escape(os.path.basename(LOG_FILE))}</code>.",
            parse_mode="HTML",
        )
        return

    # Sanitasi WAJIB sebelum log meninggalkan server.
    body = "\n".join(redact(line) for line in lines)
    heading = (
        f"<b>📜 LOG TERBARU</b>\n"
        f"<i>{len(lines)} baris terakhir"
        + (f", filter level {level}" if level else "")
        + f" — {html_lib.escape(os.path.basename(LOG_FILE))}</i>"
    )
    text = f"{heading}\n\n<pre>{html_lib.escape(body)}</pre>"
    if len(text) <= _MESSAGE_LIMIT:
        await message.reply_text(text, parse_mode="HTML")
        return

    # Terlalu panjang untuk satu pesan → kirim sebagai file .txt.
    payload = io.BytesIO(body.encode("utf-8"))
    payload.name = make_log_filename()
    await message.reply_document(
        document=payload,
        filename=payload.name,
        caption=heading,
        parse_mode="HTML",
        read_timeout=60,
        write_timeout=60,
    )
