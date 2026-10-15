"""Statistik penggunaan bot: pencatatan otomatis dan penyimpanan persisten.

Cara kerja
---------
* Setiap permintaan yang selesai diproses dicatat lewat :func:`record`.
* Handler command dibungkus :func:`track` saat didaftarkan di ``bot.py``,
  sehingga tiap pemanggilan command tercatat otomatis (``ok=False`` bila
  handler melempar exception atau memanggil :func:`mark_failed`).
* Unduhan (:func:`record` dari ``handle_message``) dan konversi dokumen
  memanggil :func:`record` langsung karena mereka tahu hasil operasinya
  (berhasil/gagal) secara akurat — wrapper ``track`` tidak menghitung ulang
  bila handler sudah mencatat sendiri.
* Data disimpan sebagai JSON atomik (tulis ``.tmp`` lalu rename) di
  ``STATS_FILE`` (default ``data/stats.json``) sehingga statistik **tetap
  bertahan setelah bot direstart**.

Pencatatan baru aktif setelah bot benar-benar berjalan (dipanggil sekali dari
``post_init`` di ``bot.py``) — mengimpor modul atau menjalankan test tidak
pernah menulis file statistik produksi.

Privasi: hanya ID Telegram pengguna (untuk menghitung pengguna unik) dan
hitungan pemakaian fitur yang disimpan. Username, teks pesan, dan data
pribadi lain **tidak** pernah disimpan.
"""

import contextvars
import functools
import html as html_lib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from config import STATS_FILE
from monitor import BOT_STARTED_AT
from utils import format_duration, wib_text

logger = logging.getLogger(__name__)

_VERSION = 1

_lock = threading.Lock()
_cache: dict | None = None
_users: set[int] = set()
_enabled = False

# Ditandai oleh handler yang sudah memanggil record() sendiri, supaya wrapper
# track() tidak menghitung permintaan yang sama dua kali.
_ALREADY_RECORDED = contextvars.ContextVar("stats_already_recorded", default=False)
# Ditandai oleh handler yang menangkap error sumber sendiri (mis. MangaDex
# error) supaya wrapper track() menghitungnya gagal, bukan berhasil.
_FAILED = contextvars.ContextVar("stats_failed", default=False)


def enable() -> None:
    """Aktifkan pencatatan statistik (dipanggil sekali saat bot mulai berjalan)."""
    global _enabled
    _enabled = True


def _reset() -> None:
    """Kosongkan state in-memory (untuk test; juga menonaktifkan pencatatan)."""
    global _cache, _users, _enabled
    _cache = None
    _users = set()
    _enabled = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_data() -> dict:
    return {
        "version": _VERSION,
        "since": _now_iso(),
        "last_request_at": None,
        "requests": {"total": 0, "ok": 0, "failed": 0},
        "unique_users": [],
        "features": {},
    }


def _quarantine_corrupt_file() -> None:
    """Simpan file statistik rusak sebagai .corrupt (bukti), jangan ditimpa diam-diam."""
    try:
        os.replace(STATS_FILE, STATS_FILE + ".corrupt")
        logger.warning("File statistik korup; dipindah ke %s", STATS_FILE + ".corrupt")
    except OSError:
        pass


def _load_locked() -> dict:
    """Muat data dari disk sekali, lalu cache di memori."""
    global _cache, _users
    if _cache is not None:
        return _cache

    data = _empty_data()
    try:
        with open(STATS_FILE, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict) and isinstance(loaded.get("requests"), dict) \
                and isinstance(loaded.get("features"), dict):
            data.update(loaded)
            users = loaded.get("unique_users")
            _users = {int(u) for u in users} if isinstance(users, list) else set()
        else:
            raise ValueError("struktur file tidak dikenal")
    except FileNotFoundError:
        pass  # belum pernah ada → mulai dari nol
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("File statistik tidak terbaca (%s); memulai dari nol", type(exc).__name__)
        _quarantine_corrupt_file()
        data = _empty_data()
        _users = set()

    _cache = data
    return data


def _save_locked(data: dict) -> None:
    """Tulis atomik: file sementara + rename, dengan izin baca 0600."""
    data["unique_users"] = sorted(_users)
    directory = os.path.dirname(STATS_FILE)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp_path = STATS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, STATS_FILE)
    try:
        os.chmod(STATS_FILE, 0o600)
    except OSError:
        pass  # izin fleksibel bukan alasan statistik gagal disimpan


def _as_int(value) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def record(feature: str, user_id: int | None, ok: bool) -> None:
    """Catat satu permintaan yang selesai diproses.

    Tidak pernah melempar error: kegagalan pencatatan hanya dicatat di log
    dan tidak pernah mengganggu proses download/konversi yang sedang berjalan.
    """
    # Menandai konteks pemanggilan ini supaya wrapper track() tidak menghitung
    # permintaan yang sama dua kali.
    _ALREADY_RECORDED.set(True)
    if not _enabled:
        return
    try:
        with _lock:
            data = _load_locked()
            requests = data.setdefault("requests", {"total": 0, "ok": 0, "failed": 0})
            key = "ok" if ok else "failed"
            requests["total"] = _as_int(requests.get("total")) + 1
            requests[key] = _as_int(requests.get(key)) + 1

            if user_id is not None:
                try:
                    _users.add(int(user_id))
                except (TypeError, ValueError):
                    pass

            features = data.setdefault("features", {})
            entry = features.setdefault(feature, {"total": 0, "ok": 0, "failed": 0})
            entry["total"] = _as_int(entry.get("total")) + 1
            entry[key] = _as_int(entry.get(key)) + 1

            data["last_request_at"] = _now_iso()
            _save_locked(data)
    except Exception:
        # Statistik tidak boleh menggagalkan request pengguna.
        logger.exception("Gagal mencatat statistik untuk fitur %r", feature)


def mark_failed() -> None:
    """Tandai permintaan yang sedang berjalan sebagai gagal.

    Dipanggil dari dalam handler yang menangkap error sumber sendiri (mis.
    MangaDex tidak terjangkau) supaya wrapper :func:`track` tidak menghitungnya
    sebagai berhasil.
    """
    _FAILED.set(True)


def track(feature: str, callback):
    """Bungkus handler Telegram agar pemanggilannya tercatat sebagai satu permintaan.

    Handler yang sudah memanggil :func:`record` sendiri dianggap tercatat dan
    tidak dihitung ulang. Exception tetap diteruskan ke error handler bot.
    """

    @functools.wraps(callback)
    async def wrapper(update, context):
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
        _ALREADY_RECORDED.set(False)
        _FAILED.set(False)
        ok = True
        try:
            return await callback(update, context)
        except Exception:
            ok = False
            raise
        finally:
            if not _ALREADY_RECORDED.get():
                record(feature, user_id, ok and not _FAILED.get())

    # Penanda agar record() dari dalam handler mengenali konteks pemanggilan ini.
    return wrapper


def snapshot() -> dict:
    """Salinan data statistik untuk ditampilkan (aman dipanggil dari handler)."""
    with _lock:
        data = _load_locked()
        return json.loads(json.dumps(data))


# ---------------------------------------------------------------------------
# Command /stats
# ---------------------------------------------------------------------------

# Urutan tampil per kategori — hanya fitur yang benar-benar ada di project.
_FEATURE_GROUPS = (
    ("📥 Unduhan media", (
        ("tiktok", "TikTok"),
        ("instagram", "Instagram"),
        ("pinterest", "Pinterest"),
    )),
    ("📄 Konversi dokumen", (
        ("pdf", "PDF → Word"),
        ("word", "Word → PDF"),
    )),
    ("🔎 Info & komik", (
        ("anime", "/anime"),
        ("manga", "/manga"),
        ("manhwa", "/manhwa"),
        ("comic", "/comic"),
        ("chapter", "/chapter"),
        ("read", "/read"),
        ("mangainfo", "/mangainfo"),
        ("mangafeature", "/mangafeature"),
    )),
    ("⚙️ Perintah & monitoring", (
        ("start", "/start"),
        ("feature", "/feature"),
        ("restart", "/restart"),
        ("status", "/status"),
        ("health", "/health"),
        ("logs", "/logs"),
        ("stats", "/stats"),
    )),
)


def _format_ts(value) -> str:
    """ISO timestamp (UTC) → waktu WIB yang mudah dibaca; gagal parse → nilai asli."""
    if not value:
        return "belum ada"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return wib_text(parsed, "%d %b %Y %H:%M")
    except ValueError:
        return str(value)


def _feature_line(label: str, entry: dict) -> str:
    total = _as_int(entry.get("total"))
    ok = _as_int(entry.get("ok"))
    failed = _as_int(entry.get("failed"))
    return f"• {label}: <b>{total:,}</b> — {ok:,} ✓ / {failed:,} ✗"


def _build_stats_text(data: dict, bot_uptime: str) -> str:
    requests = data.get("requests") or {}
    total = _as_int(requests.get("total"))
    ok = _as_int(requests.get("ok"))
    failed = _as_int(requests.get("failed"))
    features = data.get("features") or {}
    unique = len(data.get("unique_users") or [])

    lines = ["<b>📊 STATISTIK BOT</b>",
             f"<i>Sejak {html_lib.escape(_format_ts(data.get('since')))} — "
             "dihitung otomatis, tersimpan persisten (bertahan setelah restart).</i>",
             "",
             "<b>📨 Permintaan</b>"]
    if total:
        lines += [
            f"• Total diproses: <b>{total:,}</b>",
            f"• Berhasil: <b>{ok:,}</b> ({ok * 100.0 / total:.1f}%)",
            f"• Gagal: <b>{failed:,}</b>",
            f"• Pengguna unik: <b>{unique:,}</b>",
            f"• Terakhir diproses: {html_lib.escape(_format_ts(data.get('last_request_at')))}",
        ]
    else:
        lines += [
            "• Belum ada permintaan tercatat sejak fitur statistik diaktifkan.",
            f"• Pengguna unik: <b>{unique:,}</b>",
        ]

    known: set[str] = set()
    for title, mapping in _FEATURE_GROUPS:
        rows = [(label, features[name]) for name, label in mapping
                if isinstance(features.get(name), dict)]
        known.update(name for name, _ in mapping)
        if not rows:
            continue
        lines += ["", f"<b>{html_lib.escape(title)}</b>"]
        lines += [_feature_line(html_lib.escape(label), entry)
                  for label, entry in rows]

    # Fitur yang belum masuk grup tampilan (jangan sampai hilang begitu saja)
    extra = sorted(k for k, v in features.items()
                   if k not in known and isinstance(v, dict))
    if extra:
        lines += ["", "<b>🔌 Lainnya</b>"]
        lines += [_feature_line(f"<code>{html_lib.escape(name)}</code>", features[name])
                  for name in extra]

    lines += ["", f"<i>Bot uptime: {bot_uptime}</i>"]
    return "\n".join(lines)


async def stats_cmd(update, context) -> None:
    """Tampilkan statistik penggunaan bot."""
    text = _build_stats_text(snapshot(), format_duration(time.time() - BOT_STARTED_AT))
    await update.message.reply_text(
        text, parse_mode="HTML", disable_web_page_preview=True
    )
