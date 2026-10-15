"""Server monitor untuk bot: command /status dan /health.

/status
    Kondisi resource server secara real-time: uptime server, pemakaian CPU,
    RAM, disk, uptime proses bot, dan status bot. Semua angka berasal dari
    pengukuran aktual (/proc, shutil.disk_usage), bukan nilai statis.
    Indikator NORMAL / WARNING / CRITICAL memakai ambang batas dari config.

/health
    Kesehatan bot dan dependensinya: proses bot, direktori kerja (unduhan,
    statistik, log), ruang disk, serta koneksi ke layanan eksternal utama
    (Telegram, MangaDex, MyAnimeList, dan iLovePDF bila dikonfigurasi).
    Setiap pemeriksaan eksternal memakai timeout sehingga command tidak
    pernah menggantung, dan kegagalan eksternal ditandai terpisah dari
    kegagalan internal.

Efisiensi: pengukuran yang butuh waktu (sampling CPU 0,25 detik, I/O direktori)
dijalankan lewat asyncio.to_thread, jadi event loop bot tetap responsif untuk
memproses download/scraping/konversi selama /status atau /health berjalan.
"""

import asyncio
import html as html_lib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass

import httpx

from config import (
    BASE_DIR,
    CPU_CRITICAL,
    CPU_WARNING,
    DISK_CRITICAL,
    DISK_WARNING,
    DOWNLOAD_DIR,
    HEALTH_TIMEOUT,
    ILOVEPDF_PUBLIC_KEY,
    ILOVEPDF_SECRET_KEY,
    LOG_FILE,
    MIN_FREE_DISK_MB,
    RAM_CRITICAL,
    RAM_WARNING,
    STATS_FILE,
)
from mal import MAL_HEADERS
from utils import format_duration, human_bytes, wib_text

logger = logging.getLogger(__name__)

# Waktu proses bot dimulai (impor modul terjadi tepat saat proses dibuat)
BOT_STARTED_AT = time.time()

CPU_SAMPLE_INTERVAL = 0.25  # detik sampling CPU — dijalankan di thread
_DISK_FAILED_MB = 100       # sisa disk di bawah ini dianggap FAILED (MB)

STATUS_EMOJI = {"OK": "✅", "WARNING": "⚠️", "FAILED": "❌", "SKIP": "⏭️"}
_LEVEL_MARKER = {
    "NORMAL": "🟢 <b>[NORMAL]</b>",
    "WARNING": "🟠 <b>[WARNING]</b>",
    "CRITICAL": "🔴 <b>[CRITICAL]</b>",
}


def usage_level(percent: float | None, warning: int, critical: int) -> str:
    """NORMAL / WARNING / CRITICAL berdasarkan ambang batas dari config."""
    if percent is None:
        return "NORMAL"
    if percent >= critical:
        return "CRITICAL"
    if percent >= warning:
        return "WARNING"
    return "NORMAL"


# ---------------------------------------------------------------------------
# Pengukuran (semua blocking → jalankan lewat asyncio.to_thread)
# ---------------------------------------------------------------------------

def _cpu_times() -> tuple[int, int] | None:
    """(busy, total) tick CPU agregat semua core dari /proc/stat."""
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            line = fh.readline()
    except OSError:
        return None
    try:
        values = [int(v) for v in line.split()[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    return sum(values) - idle, sum(values)


def sample_cpu(interval: float = CPU_SAMPLE_INTERVAL) -> float | None:
    """Persentase pemakaian CPU semua core selama `interval` detik.

    Blocking (time.sleep) — hanya dipanggil lewat asyncio.to_thread agar bot
    tetap merespons selama sampling berlangsung.
    """
    first = _cpu_times()
    if first is None:
        return None
    time.sleep(interval)
    second = _cpu_times()
    if second is None:
        return None
    delta_total = second[1] - first[1]
    if delta_total <= 0:
        return None
    percent = 100.0 * (second[0] - first[0]) / delta_total
    return max(0.0, min(100.0, percent))


def read_memory() -> dict | None:
    """MemTotal, MemAvailable, dan pemakaian (byte) dari /proc/meminfo."""
    total = available = free = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key == "MemTotal":
                    total = int(rest.split()[0]) * 1024
                elif key == "MemAvailable":
                    available = int(rest.split()[0]) * 1024
                elif key == "MemFree":
                    free = int(rest.split()[0]) * 1024
                if total is not None and (available is not None or free is not None):
                    break
    except (OSError, ValueError, IndexError):
        return None
    if total is None or total <= 0:
        return None
    if available is None:
        available = free  # kernel sangat lama tanpa MemAvailable
    if available is None:
        return None
    available = min(max(available, 0), total)
    return {"total": total, "available": available, "used": total - available}


def read_uptime() -> float | None:
    """Detik sejak server boot (/proc/uptime)."""
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_disk(path: str) -> dict | None:
    """Info kapasitas filesystem yang menampung `path`."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": usage.total, "used": usage.used, "free": usage.free}


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def _build_status_text(cpu, memory, disk, uptime) -> str:
    checked_at = wib_text()  # selalu WIB (UTC+07:00), bukan timezone server
    lines = [
        "<b>🖥 STATUS SERVER</b>",
        "",
        "<b>🤖 Bot:</b> <b>ONLINE</b> ✅ — merespons command ini",
        f"<b>⏱ Bot uptime:</b> {format_duration(time.time() - BOT_STARTED_AT)}",
        f"<b>🐧 Server uptime:</b> {format_duration(uptime)}",
        "",
    ]

    if cpu is None:
        lines.append("<b>🧠 CPU:</b> tidak tersedia (n/a)")
    else:
        level = usage_level(cpu, CPU_WARNING, CPU_CRITICAL)
        lines.append(f"<b>🧠 CPU:</b> {cpu:.1f}% {_LEVEL_MARKER[level]}")

    if memory:
        percent = 100.0 * memory["used"] / memory["total"]
        level = usage_level(percent, RAM_WARNING, RAM_CRITICAL)
        lines.append(
            f"<b>💾 RAM:</b> {human_bytes(memory['used'])} / {human_bytes(memory['total'])} "
            f"({percent:.1f}%) — tersedia {human_bytes(memory['available'])} "
            f"{_LEVEL_MARKER[level]}"
        )
    else:
        lines.append("<b>💾 RAM:</b> tidak tersedia (n/a)")

    if disk and disk["total"]:
        percent = 100.0 * disk["used"] / disk["total"]
        level = usage_level(percent, DISK_WARNING, DISK_CRITICAL)
        lines.append(
            f"<b>💿 Disk:</b> {human_bytes(disk['used'])} / {human_bytes(disk['total'])} "
            f"({percent:.1f}%) — tersedia {human_bytes(disk['free'])} "
            f"{_LEVEL_MARKER[level]}"
        )
    else:
        lines.append("<b>💿 Disk:</b> tidak tersedia (n/a)")

    lines += ["", f"<i>Diperiksa: {checked_at}</i>"]
    return "\n".join(lines)


async def status_cmd(update, context) -> None:
    """Tampilkan kondisi resource server secara real-time."""
    cpu, memory, disk, uptime = await asyncio.gather(
        asyncio.to_thread(sample_cpu),
        asyncio.to_thread(read_memory),
        asyncio.to_thread(read_disk, BASE_DIR),
        asyncio.to_thread(read_uptime),
    )
    await update.message.reply_text(
        _build_status_text(cpu, memory, disk, uptime),
        parse_mode="HTML", disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    """Hasil satu pemeriksaan kesehatan."""
    name: str
    status: str   # OK | WARNING | FAILED | SKIP
    detail: str
    external: bool = False  # True → dependensi luar, bukan bug/gangguan internal


def _check_process() -> Check:
    return Check(
        "Proses bot", "OK",
        f"berjalan dan merespons (uptime {format_duration(time.time() - BOT_STARTED_AT)})",
    )


def _check_writable_dir(name: str, path: str) -> Check:
    """Direktori tersedia dan bisa ditulis (dibuat dulu bila belum ada)."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as exc:
        return Check(name, "FAILED",
                     f"{path} tidak dapat dibuat ({exc.strerror or type(exc).__name__})")
    if not os.path.isdir(path):
        return Check(name, "FAILED", f"{path} bukan direktori")
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".health_", dir=path)
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"ok")
        os.remove(tmp_path)
    except OSError as exc:
        return Check(name, "FAILED",
                     f"{path} tidak dapat ditulis ({exc.strerror or type(exc).__name__})")
    return Check(name, "OK", f"{path} dapat ditulis")


def _check_disk_space(name: str, path: str) -> Check:
    """Ruang disk tersedia untuk operasi bot (unduhan/konversi/statistik)."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return Check(name, "FAILED",
                     f"info disk tidak terbaca ({exc.strerror or type(exc).__name__})")
    free_mb = usage.free // (1024 * 1024)
    detail = f"{human_bytes(usage.free)} tersedia dari {human_bytes(usage.total)}"
    if free_mb < _DISK_FAILED_MB:
        return Check(name, "FAILED",
                     f"ruang tersisa sangat sedikit — {detail} "
                     f"(minimal {_DISK_FAILED_MB} MB)")
    if free_mb < MIN_FREE_DISK_MB:
        return Check(name, "WARNING",
                     f"ruang mulai menipis — {detail} (batas {MIN_FREE_DISK_MB} MB)")
    return Check(name, "OK", detail)


async def _check_telegram(context) -> Check:
    """Koneksi ke Telegram API (dependensi inti bot) dengan timeout."""
    bot = getattr(context, "bot", None)
    if bot is None:
        return Check("Telegram API", "FAILED", "objek bot tidak tersedia", external=True)
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT):
            me = await bot.get_me()
    except TimeoutError:
        return Check("Telegram API", "FAILED",
                     f"tidak merespons dalam {HEALTH_TIMEOUT:g} detik", external=True)
    except Exception as exc:
        logger.warning("Health: Telegram API gagal (%s)", type(exc).__name__)
        return Check("Telegram API", "FAILED",
                     f"tidak dapat dihubungi ({type(exc).__name__})", external=True)
    return Check("Telegram API", "OK", f"terhubung sebagai @{me.username}", external=True)


async def _check_http(name: str, url: str, headers: dict | None = None) -> Check:
    """GET ringan ke layanan eksternal; semua kegagalan ditandai eksternal."""
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT):
            async with httpx.AsyncClient(
                timeout=HEALTH_TIMEOUT, follow_redirects=True, headers=headers,
            ) as client:
                started = time.monotonic()
                response = await client.get(url)
                elapsed_ms = int((time.monotonic() - started) * 1000)
    except TimeoutError:
        return Check(name, "FAILED",
                     f"tidak merespons dalam {HEALTH_TIMEOUT:g} detik", external=True)
    except httpx.TimeoutException:
        return Check(name, "FAILED",
                     f"tidak merespons dalam {HEALTH_TIMEOUT:g} detik", external=True)
    except httpx.HTTPError as exc:
        return Check(name, "FAILED", f"gagal terhubung ({type(exc).__name__})",
                     external=True)
    except Exception as exc:  # kegagalan tak terduga jangan pernah menjatuhkan command
        logger.warning("Health: pemeriksaan %s gagal tak terduga (%s)",
                       name, type(exc).__name__)
        return Check(name, "FAILED", f"gagal ({type(exc).__name__})", external=True)
    if response.status_code >= 500:
        return Check(name, "WARNING", f"server bermasalah (HTTP {response.status_code})",
                     external=True)
    return Check(name, "OK", f"merespons (HTTP {response.status_code}, {elapsed_ms} ms)",
                 external=True)


async def _check_ilovepdf() -> Check:
    """iLovePDF hanya dicek bila dikonfigurasi — kalau tidak, /word memang nonaktif."""
    if not (ILOVEPDF_PUBLIC_KEY and ILOVEPDF_SECRET_KEY):
        return Check("iLovePDF API", "SKIP",
                     "belum dikonfigurasi — fitur /word nonaktif")
    return await _check_http("iLovePDF API", "https://api.ilovepdf.com/")


async def run_checks(context) -> list[Check]:
    """Jalankan semua pemeriksaan secara paralel (internal + eksternal)."""
    process = _check_process()  # sinkron & tanpa I/O — tidak perlu thread
    checks = await asyncio.gather(
        # Database: project ini tidak memakai database — tidak ada cek database.
        asyncio.to_thread(_check_writable_dir, "Direktori unduhan", DOWNLOAD_DIR),
        asyncio.to_thread(_check_writable_dir, "Direktori statistik",
                          os.path.dirname(STATS_FILE) or "."),
        asyncio.to_thread(_check_writable_dir, "Direktori log",
                          os.path.dirname(LOG_FILE) or "."),
        asyncio.to_thread(_check_disk_space, "Ruang disk (unduhan)", DOWNLOAD_DIR),
        asyncio.to_thread(_check_disk_space, "Ruang disk (proyek)", BASE_DIR),
        _check_telegram(context),
        _check_http("MangaDex API", "https://api.mangadex.org/ping"),
        _check_http("MyAnimeList", "https://myanimelist.net/", headers=MAL_HEADERS),
        _check_ilovepdf(),
    )
    return [process, *checks]


def _build_health_text(checks: list[Check]) -> str:
    failed = [c for c in checks if c.status == "FAILED"]
    warned = [c for c in checks if c.status == "WARNING"]
    internal_failed = [c for c in failed if not c.external]
    external_failed = [c for c in failed if c.external]

    if internal_failed:
        overall = "❌ ADA MASALAH"
    elif external_failed:
        overall = "⚠️ MASALAH LAYANAN EKSTERNAL — bot tetap berjalan"
    elif warned:
        overall = "⚠️ PERINGATAN"
    else:
        overall = "✅ SEHAT"

    counts: dict[str, int] = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    summary = ", ".join(f"{value} {key}" for key, value in counts.items())

    lines = [
        "<b>🩺 HEALTH CHECK</b>",
        "",
        f"<b>Status:</b> {overall}",
        f"<b>Pemeriksaan:</b> {len(checks)} — {summary}",
        "",
    ]
    for check in checks:
        emoji = STATUS_EMOJI.get(check.status, "❔")
        line = (f"{emoji} <b>{html_lib.escape(check.name)}:</b> {check.status} — "
                f"{html_lib.escape(check.detail)}")
        if check.status in ("FAILED", "WARNING"):
            line += " <i>[layanan eksternal]</i>" if check.external else " <i>[internal]</i>"
        lines.append(line)
    lines += ["", f"<i>Diperiksa: {wib_text()}</i>"]
    return "\n".join(lines)


async def health_cmd(update, context) -> None:
    """Cek kesehatan bot dan dependensinya, dengan timeout tiap pemeriksaan."""
    checks = await run_checks(context)
    await update.message.reply_text(
        _build_health_text(checks),
        parse_mode="HTML", disable_web_page_preview=True,
    )
