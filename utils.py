import re
import time
import uuid
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Zona waktu — semua jam yang ditampilkan ke user selalu WIB (UTC+07:00).
# Offset tetap dipakai karena WIB tidak punya daylight saving dan supaya
# tidak bergantung pada timezone sistem (server produksi berjalan dalam UTC).
# ---------------------------------------------------------------------------
WIB = timezone(timedelta(hours=7), name="WIB")

TIKTOK_DOMAINS = (
    "tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
)

PINTEREST_DOMAINS = (
    "pinterest.com",
    "www.pinterest.com",
    "pin.it",
    "id.pinterest.com",
)

INSTAGRAM_DOMAINS = (
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "ddinstagram.com",
    "www.ddinstagram.com",
)

URL_PATTERN = re.compile(r"https?://[^\s]+")


def extract_url(text: str) -> str | None:
    """Extract the first URL found in a message."""
    match = URL_PATTERN.search(text or "")
    return match.group(0) if match else None


def is_tiktok_url(url: str) -> bool:
    """Check whether a URL belongs to a supported TikTok domain."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in TIKTOK_DOMAINS)


def is_instagram_url(url: str) -> bool:
    """Check whether a URL belongs to a supported Instagram domain."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in INSTAGRAM_DOMAINS)


def is_pinterest_url(url: str) -> bool:
    """Check whether a URL belongs to a supported Pinterest domain."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in PINTEREST_DOMAINS)


def sanitize_filename(name: str) -> str:
    """Strip unsafe characters from a filename."""
    name = re.sub(r"[^\w\-.]", "_", name)
    return name[:100] if len(name) > 100 else name


def unique_job_id() -> str:
    return uuid.uuid4().hex[:12]


class RateLimiter:
    """Simple in-memory sliding-window rate limiter per user."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[int, deque] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        q = self._requests[user_id]
        while q and now - q[0] > self.window_seconds:
            q.popleft()
        if len(q) >= self.max_requests:
            return False
        q.append(now)
        return True

    def retry_after(self, user_id: int) -> int:
        q = self._requests[user_id]
        if not q:
            return 0
        elapsed = time.monotonic() - q[0]
        return max(0, int(self.window_seconds - elapsed))

class ProgressReporter:
    """Throttled progress bar updater for a Telegram status message."""

    def __init__(self, edit_func, min_interval: float = 1.5):
        self.edit_func = edit_func  # async callable(text: str)
        self.min_interval = min_interval
        self._last_time = 0.0
        self._last_percent: int | None = -1

    async def update(self, percent: int | None, downloaded_mb: float = 0.0) -> None:
        now = time.monotonic()

        if percent == self._last_percent:
            return
        if percent is not None and percent < 100 and (now - self._last_time) < self.min_interval:
            return

        self._last_time = now
        self._last_percent = percent

        if percent is not None:
            bar = self._make_bar(percent)
            text = f"Mengambil media...\n{bar} {percent}%"
        else:
            text = f"Mengambil media...\n({downloaded_mb:.1f} MB terunduh)"

        try:
            await self.edit_func(text)
        except Exception:
            pass  # ignore "message not modified" or transient edit errors

    @staticmethod
    def _make_bar(percent: int, length: int = 12) -> str:
        filled = int(length * percent / 100)
        return "[" + "█" * filled + "░" * (length - filled) + "]"


def now_wib() -> datetime:
    """Waktu sekarang dalam WIB (UTC+07:00)."""
    return datetime.now(WIB)


def wib_text(moment: datetime | None = None,
             fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format waktu → ``"2026-10-15 11:20 WIB"``; default: waktu sekarang.

    ``moment`` boleh naive (dianggap sudah WIB) atau bertz — semuanya
    dikonversi dulu ke WIB sebelum diformat.
    """
    when = moment if moment is not None else now_wib()
    if when.tzinfo is None:
        when = when.replace(tzinfo=WIB)
    return when.astimezone(WIB).strftime(fmt) + " WIB"


def format_duration(seconds: float | None) -> str:
    """Format durasi (detik) → '12d 4h 33m 09s'; None → 'n/a'."""
    if seconds is None:
        return "n/a"
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    if days or hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs:02d}s")
    return " ".join(parts)


def human_bytes(size: float | None) -> str:
    """Format byte → '7.8 GB' (basis 1024); None → 'n/a'."""
    if size is None:
        return "n/a"
    value = float(size)
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1024
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}"
