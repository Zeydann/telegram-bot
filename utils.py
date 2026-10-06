import re
import time
import uuid
import logging
from collections import defaultdict, deque
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TIKTOK_DOMAINS = (
    "tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
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
