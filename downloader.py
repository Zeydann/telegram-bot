import json
import re
import httpx
import os
import asyncio
import logging
import shutil
from dataclasses import dataclass, field

import yt_dlp

from utils import unique_job_id
from config import DOWNLOAD_DIR, MAX_FILE_SIZE

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when a TikTok download fails for a known reason."""


class _YtDlpQuietLogger:
    """Silence yt-dlp's stderr output (fallback handles photo posts)."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


@dataclass
class DownloadResult:
    media_type: str  # "video" or "images"
    files: list[str] = field(default_factory=list)
    author: str | None = None
    description: str | None = None


def _run_ytdlp(url: str, job_dir: str, progress_hook=None) -> dict:
    """Blocking yt-dlp extraction; run inside a thread."""
    outtmpl = os.path.join(job_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo+bestaudio/best/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
    }
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return info

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


async def _resolve_url(url: str) -> str:
    """Follow redirects (for vt.tiktok.com / vm.tiktok.com short links)."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers={"User-Agent": MOBILE_UA}) as client:
        resp = await client.get(url)
        return str(resp.url)

async def _scrape_photo_post(url: str, job_dir: str, progress_callback=None) -> DownloadResult:
    """Fallback for TikTok photo/slideshow posts not supported by yt-dlp."""
    resolved_url = await _resolve_url(url)

    headers = {
        "User-Agent": MOBILE_UA,
        "Referer": "https://www.tiktok.com/",
    }

    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        resp = await client.get(resolved_url)
        resp.raise_for_status()
        html = resp.text

    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if not match:
        raise DownloadError("Tidak dapat membaca data postingan ini.")

    try:
        data = json.loads(match.group(1))
        default_scope = data["__DEFAULT_SCOPE__"]
        item_struct = None
        for key, value in default_scope.items():
            if "itemInfo" in value and "itemStruct" in value["itemInfo"]:
                item_struct = value["itemInfo"]["itemStruct"]
                break
        if not item_struct:
            raise KeyError("itemStruct not found")

        image_post = item_struct.get("imagePost")
        if not image_post or "images" not in image_post:
            raise DownloadError("Postingan ini bukan slideshow foto.")

        image_urls = [
            img["imageURL"]["urlList"][0]
            for img in image_post["images"]
            if img.get("imageURL", {}).get("urlList")
        ]
        author = item_struct.get("author", {}).get("uniqueId")
        description = item_struct.get("desc")
    except (KeyError, json.JSONDecodeError, TypeError) as e:
        logger.error("Failed to parse TikTok photo data: %s", e)
        raise DownloadError("Gagal membaca data foto dari postingan ini.")

    if not image_urls:
        raise DownloadError("Tidak ada gambar yang ditemukan dalam postingan ini.")

    files = []
    total = len(image_urls)
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        for i, img_url in enumerate(image_urls):
            try:
                r = await client.get(img_url)
                r.raise_for_status()
                path = os.path.join(job_dir, f"image_{i}.jpg")
                with open(path, "wb") as f:
                    f.write(r.content)
                files.append(path)
            except Exception as e:
                logger.warning("Failed to download image %d: %s", i, e)

            if progress_callback:
                percent = int((i + 1) / total * 100)
                await progress_callback(percent, 0.0)

    if not files:
        raise DownloadError("Gagal mengunduh gambar dari postingan ini.")

    return DownloadResult(
        media_type="images",
        files=files,
        author=author,
        description=description,
    )

async def download_tiktok(url: str, progress_callback=None) -> DownloadResult:
    """Download a TikTok URL and return a structured result."""
    job_id = unique_job_id()
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    loop = asyncio.get_running_loop()

    def sync_progress_hook(d):
        if progress_callback is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = int(downloaded / total * 100) if total else None
            downloaded_mb = downloaded / (1024 * 1024)
            asyncio.run_coroutine_threadsafe(
                progress_callback(percent, downloaded_mb), loop
            )
        elif status == "finished":
            asyncio.run_coroutine_threadsafe(progress_callback(100, 0.0), loop)

    hook = sync_progress_hook if progress_callback else None

    try:
        info = await loop.run_in_executor(None, _run_ytdlp, url, job_dir, hook)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "unsupported url" in msg and "/photo/" in msg:
            try:
                return await _scrape_photo_post(url, job_dir, progress_callback)
            except DownloadError:
                cleanup_job(job_dir)
                raise
            except Exception:
                cleanup_job(job_dir)
                logger.exception("Photo fallback failed for %s", url)
                raise DownloadError("Gagal mengambil foto dari postingan ini.")
        cleanup_job(job_dir)
        if "private" in msg:
            raise DownloadError("Video ini bersifat private atau tidak dapat diakses.")
        if "not available" in msg or "removed" in msg or "404" in msg:
            raise DownloadError("Video sudah dihapus atau tidak ditemukan.")
        if "rate" in msg or "429" in msg:
            raise DownloadError("TikTok sedang membatasi request. Coba lagi beberapa saat lagi.")
        logger.error("yt-dlp download error for %s: %s", url, e)
        raise DownloadError("Gagal mengambil media dari link tersebut.")
    except Exception:
        cleanup_job(job_dir)
        logger.exception("Unexpected error downloading %s", url)
        raise DownloadError("Terjadi kesalahan saat memproses link.")

    author = info.get("uploader") or info.get("creator")
    description = info.get("description") or info.get("title")

    files: list[str] = []
    for root, _, names in os.walk(job_dir):
        for name in names:
            files.append(os.path.join(root, name))

    if not files:
        cleanup_job(job_dir)
        raise DownloadError("Media tidak ditemukan untuk link ini.")

    video_exts = {".mp4", ".mov", ".webm", ".mkv"}
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}

    video_files = [f for f in files if os.path.splitext(f)[1].lower() in video_exts]
    image_files = [f for f in files if os.path.splitext(f)[1].lower() in image_exts]

    if video_files:
        video_path = video_files[0]
        size = os.path.getsize(video_path)
        if size > MAX_FILE_SIZE:
            cleanup_job(job_dir)
            raise DownloadError(
                f"Ukuran video ({size // (1024*1024)}MB) melebihi batas yang diizinkan."
            )
        return DownloadResult(
            media_type="video",
            files=[video_path],
            author=author,
            description=description,
        )

    if image_files:
        return DownloadResult(
            media_type="images",
            files=image_files,
            author=author,
            description=description,
        )

    cleanup_job(job_dir)
    raise DownloadError("Format media tidak dikenali.")

async def download_instagram(url: str, progress_callback=None) -> DownloadResult:
    """Download an Instagram post/reel and return a structured result."""
    job_id = unique_job_id()
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    loop = asyncio.get_running_loop()

    def sync_progress_hook(d):
        if progress_callback is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = int(downloaded / total * 100) if total else None
            downloaded_mb = downloaded / (1024 * 1024)
            asyncio.run_coroutine_threadsafe(
                progress_callback(percent, downloaded_mb), loop
            )
        elif status == "finished":
            asyncio.run_coroutine_threadsafe(progress_callback(100, 0.0), loop)

    hook = sync_progress_hook if progress_callback else None

    try:
        info = await loop.run_in_executor(None, _run_ytdlp, url, job_dir, hook)
    except yt_dlp.utils.DownloadError as e:
        cleanup_job(job_dir)
        msg = str(e).lower()
        if "login" in msg or "cookies" in msg or "not logged in" in msg:
            raise DownloadError("Postingan ini memerlukan login (private/akun terkunci).")
        if "not available" in msg or "removed" in msg or "404" in msg:
            raise DownloadError("Postingan Instagram sudah dihapus atau tidak ditemukan.")
        if "rate" in msg or "429" in msg:
            raise DownloadError("Instagram sedang membatasi request. Coba lagi beberapa saat lagi.")
        logger.error("yt-dlp download error for %s: %s", url, e)
        raise DownloadError("Gagal mengambil media dari link Instagram tersebut.")
    except Exception:
        cleanup_job(job_dir)
        logger.exception("Unexpected error downloading %s", url)
        raise DownloadError("Terjadi kesalahan saat memproses link.")

    author = info.get("uploader") or info.get("creator") or info.get("channel")
    description = info.get("description") or info.get("title")

    files: list[str] = []
    for root, _, names in os.walk(job_dir):
        for name in names:
            files.append(os.path.join(root, name))

    if not files:
        cleanup_job(job_dir)
        raise DownloadError("Media tidak ditemukan untuk link ini.")

    video_exts = {".mp4", ".mov", ".webm", ".mkv"}
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}

    video_files = [f for f in files if os.path.splitext(f)[1].lower() in video_exts]
    image_files = sorted(f for f in files if os.path.splitext(f)[1].lower() in image_exts)

    if video_files:
        video_path = video_files[0]
        size = os.path.getsize(video_path)
        if size > MAX_FILE_SIZE:
            cleanup_job(job_dir)
            raise DownloadError(
                f"Ukuran video ({size // (1024*1024)}MB) melebihi batas yang diizinkan."
            )
        return DownloadResult(
            media_type="video",
            files=[video_path],
            author=author,
            description=description,
        )

    if image_files:
        return DownloadResult(
            media_type="images",
            files=image_files,
            author=author,
            description=description,
        )

    cleanup_job(job_dir)
    raise DownloadError("Format media tidak dikenali.")


def cleanup_job(job_dir: str) -> None:
    """Remove a job's temporary directory and all its contents."""
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        logger.warning("Failed to clean up job dir: %s", job_dir)
