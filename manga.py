"""Handler Telegram untuk fitur manga & manhwa Bahasa Indonesia.

Perintah yang tersedia:
    /manga <judul>        — cari manga (Jepang) terjemahan Indonesia
    /manhwa <judul>       — cari manhwa/webtoon (Korea) terjemahan Indonesia
    /comic <judul>        — cari manga dan manhwa sekaligus
    /chapter <judul>      — daftar chapter Bahasa Indonesia
    /read <judul> <ch>    — baca chapter Indonesia
    /mangainfo <judul>    — detail judul + cover
    /mangafeature         — daftar command

Aturan Bahasa (paling penting): pencarian SELALU dimulai dari chapter dengan
metadata terjemahan ``id`` (Bahasa Indonesia); chapter Indonesia yang ada
ditampilkan lebih dulu dan tidak pernah diganti otomatis. Bila judul sama
sekali tidak punya chapter Indonesia, bot MENAWARKAN versi Bahasa Inggris
lewat tombol "Yes, English" / "No, Cancel" — chapter Inggris HANYA dicari
dan ditampilkan setelah pengguna menekan "Yes, English". Sumber data:
MangaDex (lihat ``mangadex.py``).
"""

import asyncio
import html as html_lib
import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import mangadex
from mangadex import (
    Chapter,
    MangaDexError,
    SourceRateLimited,
    Title,
    manga_url,
)
# Menandai kegagalan sumber (MangaDex) sebagai permintaan gagal di /stats
from stats import mark_failed

logger = logging.getLogger(__name__)

MAX_MEDIA_GROUP = 10          # batas Telegram per media group
MAX_PAGES_SEND = 100          # batas halaman per chapter (10 grup)
PAGE_DOWNLOAD_CONCURRENCY = 4
CHAPTER_PAGE_SIZE = 8         # jumlah entry chapter per pesan
CAPTION_LIMIT = 1024          # limit caption pesan berfoto Telegram
TEXT_LIMIT = 4096

SESSION_TTL = 1800            # tombol inline berlaku 30 menit
MAX_SESSIONS = 512

SESSIONS: dict[str, dict] = {}
_INFLIGHT: set[str] = set()   # cegah pemrosesan dobel dari tombol yang sama

# ---------------------------------------------------------------------------
# Pesan (Bahasa Indonesia)
# ---------------------------------------------------------------------------

USAGE = {
    "manga": "Pakai: /manga judul\nContoh: /manga One Piece",
    "manhwa": "Pakai: /manhwa judul\nContoh: /manhwa Tower of God",
    "comic": "Pakai: /comic judul\nContoh: /comic Solo Leveling",
    "chapter": "Pakai: /chapter judul\nContoh: /chapter One Piece",
    "read": "Pakai: /read judul chapter\nContoh: /read One Piece 10",
    "mangainfo": "Pakai: /mangainfo judul\nContoh: /mangainfo One Piece",
}

NO_ID_CHAPTERS = (
    "Tidak ada chapter Bahasa Indonesia yang tersedia untuk judul ini. "
    "Coba judul lain atau cek sumber aslinya."
)
NO_EN_CHAPTERS = (
    "Tidak ada chapter Bahasa Inggris yang tersedia untuk judul ini. "
    "Coba judul lain."
)
CANCELLED_ENGLISH = "Pencarian versi Bahasa Inggris dibatalkan."
CHAPTER_NOT_FOUND = "Chapter tidak ditemukan."
SOURCE_BUSY = "Sumber sedang tidak dapat diakses. Coba lagi nanti."
RATE_LIMITED_MSG = "Sumber sedang membatasi request. Coba lagi beberapa saat lagi."
PICK_A_TITLE = "Silakan pilih salah satu hasil pencarian."
PAGE_LOAD_FAILED = "Gagal memuat halaman chapter."
EXPIRED_SESSION = "Sesi sudah kedaluwarsa. Silakan cari ulang lewat command pencarian."

# Teks /mangafeature (satu sini + daftar di /feature pada bot.py)
MANGAFEATURE_HTML = (
    "<b>Manga &amp; Manhwa Indonesia</b>\n\n"
    "• <code>/manga &lt;judul&gt;</code> — Cari manga terjemahan Indonesia.\n"
    "• <code>/manhwa &lt;judul&gt;</code> — Cari manhwa terjemahan Indonesia.\n"
    "• <code>/comic &lt;judul&gt;</code> — Cari manga dan manhwa sekaligus.\n"
    "• <code>/chapter &lt;judul&gt;</code> — Lihat daftar chapter Bahasa Indonesia.\n"
    "• <code>/read &lt;judul&gt; &lt;chapter&gt;</code> — Baca atau buka chapter Indonesia.\n"
    "• <code>/mangainfo &lt;judul&gt;</code> — Lihat detail judul beserta cover-nya.\n"
    "• <code>/mangafeature</code> — Lihat daftar fitur manga dan manhwa.\n\n"
    "<i>Utamakan chapter Bahasa Indonesia; versi Inggris hanya ditampilkan "
    "setelah konfirmasi lewat tombol.</i>\n\n"
    "Source: MangaDex\n"
    "Website: https://mangadex.org/\n"
    "API Documentation: https://api.mangadex.org/docs/"
)


def _escape(text: str | None) -> str:
    return html_lib.escape(text or "")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 3, 0)].rsplit(" ", 1)[0]
    if not cut:
        cut = text[: max(limit - 3, 0)]
    return cut + "..."


# ---------------------------------------------------------------------------
# Label bahasa (id selalu default; en HANYA setelah konfirmasi pengguna)
# ---------------------------------------------------------------------------

LANG_NAMES = {
    mangadex.INDONESIAN_LANG: "Indonesia",
    mangadex.ENGLISH_LANG: "Inggris",
}

LANG_FLAGS = {
    mangadex.INDONESIAN_LANG: "🇮🇩",
    mangadex.ENGLISH_LANG: "🇬🇧",
}


def _lang_name(lang: str) -> str:
    return LANG_NAMES.get(lang, lang)


def _lang_flag(lang: str) -> str:
    return LANG_FLAGS.get(lang, "🌐")


def _lang_line(lang: str) -> str:
    """Baris "Bahasa: Indonesia (id)" / "Bahasa: Inggris (en)"."""
    return f"Bahasa: {_lang_name(lang)} ({lang})"


def _no_chapters_text(lang: str) -> str:
    return NO_ID_CHAPTERS if lang == mangadex.INDONESIAN_LANG else NO_EN_CHAPTERS


# ---------------------------------------------------------------------------
# Nilai English untuk kartu detail /mangainfo (label kartu memakai English)
# ---------------------------------------------------------------------------

INFO_LANG_NAMES = {
    mangadex.INDONESIAN_LANG: "Indonesian",
    mangadex.ENGLISH_LANG: "English",
}

# nilai status sudah dipetakan mangadex.STATUS_ID ke Bahasa Indonesia
INFO_STATUS_NAMES = {
    "Berjalan": "Ongoing",
    "Tamat": "Completed",
    "Dibatalkan": "Cancelled",
    "Hiatus": "Hiatus",
    "Tidak diketahui": "Unknown",
}


def _info_status(status: str) -> str:
    return INFO_STATUS_NAMES.get(status, status)


# ---------------------------------------------------------------------------
# Sesi tombol inline
# ---------------------------------------------------------------------------

def _new_session(**payload) -> str:
    now = time.monotonic()
    for key in [k for k, s in SESSIONS.items() if now > s.get("exp", 0)]:
        SESSIONS.pop(key, None)
    while len(SESSIONS) >= MAX_SESSIONS:
        SESSIONS.pop(next(iter(SESSIONS)))
    token = uuid.uuid4().hex[:10]
    payload["exp"] = now + SESSION_TTL
    SESSIONS[token] = payload
    return token


def _get_session(token: str) -> dict | None:
    session = SESSIONS.get(token)
    if not session:
        return None
    if time.monotonic() > session.get("exp", 0):
        SESSIONS.pop(token, None)
        return None
    return session


def _callback(action: str, token: str, extra: str = "") -> str:
    return f"mg|{action}|{token}|{extra}"


# ---------------------------------------------------------------------------
# Penyusun pesan
# ---------------------------------------------------------------------------

def _title_caption(title: Title, show_category: bool = False,
                   budget: int = CAPTION_LIMIT,
                   lang: str = mangadex.INDONESIAN_LANG) -> str:
    """Caption ringkas untuk satu hasil pencarian (foto + info + tombol)."""
    head = f"<b>{_escape(title.title)}</b>"
    if show_category and title.category:
        head += f" ({_escape(title.category.capitalize())})"

    lines = [head]
    if title.indonesian_title and title.indonesian_title != title.title:
        lines.append(f"🇮🇩 Judul Indonesia: {_escape(title.indonesian_title)}")
    if title.alt_titles:
        lines.append("📖 Alternatif: " + _escape(", ".join(title.alt_titles[:3])))
    if title.genres:
        lines.append("🎭 Genre: " + _escape(", ".join(title.genres[:6])))
    lines.append(f"📢 Status: {_escape(title.status)}")
    lines.append(f"🔤 Bahasa asal: {_escape(title.original_language)}")
    if title.id_chapter_count is not None:
        latest = f" — terbaru: {_escape(title.latest_id_chapter)}" if title.latest_id_chapter else ""
        lines.append(f"📚 Chapter {_lang_name(lang)}: {title.id_chapter_count}{latest}")

    fixed = "\n".join(lines)
    link = f'<a href="{title.url}">Buka di MangaDex</a>'
    credit = "Source: MangaDex"

    synopsis = title.synopsis_id or title.synopsis
    block = ""
    if synopsis:
        # sisa ruang untuk sinopsis dalam blok yang bisa dibuka
        budget_left = budget - len(fixed) - len(link) - len(credit) - len(
            "\n\n<blockquote expandable></blockquote>\n\n"
        )
        if budget_left > 40:
            block = f"<blockquote expandable>{_escape(_truncate(synopsis, budget_left))}</blockquote>"

    parts = [fixed]
    if block:
        parts.append(block)
    parts.append(link)
    parts.append(credit)
    return _truncate("\n".join(parts), budget)


def _truncate_escaped(text: str, limit: int) -> str:
    """Escape HTML lalu potong aman: tidak memotong entitas ``&amp;``/tag.

    Dipakai untuk konten yang masuk ke kartu info, supaya panjang hasil escape
    tetap terprediksi dan parsing HTML Telegram tidak rusak.
    """
    escaped = html_lib.escape(text or "")
    if len(escaped) <= limit:
        return escaped
    cut = escaped[: max(limit - 3, 0)]
    if "&" in cut:  # buang sisa entitas yang terpotong ("&am")
        head, _, tail = cut.rpartition("&")
        if ";" not in tail:
            cut = head
    parts = cut.rsplit(None, 1)  # potong di batas kata terakhir
    if parts and parts[0]:
        cut = parts[0]
    return cut.rstrip() + "..."


def _short_list(items: list[str], limit: int, sep: str = ", ") -> str:
    """Daftar elemen pendek (dibatasi jumlah & panjangnya) agar kartu muat."""
    shown = [_truncate_escaped(item, 40) for item in items[:limit]]
    text = sep.join(shown)
    extra = len(items) - limit
    if extra > 0:
        text += f" +{extra} more"
    return text


def _clean_synopsis(text: str) -> str:
    """Rapikan sinopsis dari sumber: buang spasi tepi & rangkaian baris kosong.

    Struktur paragraf asli dipertahankan (termasuk baris baru di dalam satu
    paragraf), hanya spasi tepi dan baris kosong berlebihan yang dibersihkan.
    """
    paragraphs: list[str] = []
    buffer: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.split())  # buang spasi tepi & rangkaian spasi
        if line:
            buffer.append(line)
        elif buffer:
            paragraphs.append("\n".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append("\n".join(buffer))
    return "\n\n".join(paragraphs)


def _info_text(title: Title, latest: str | None, languages: list[str],
               budget: int = CAPTION_LIMIT) -> str:
    """Kartu detail /mangainfo — format & label English sesuai contoh:

        📚 Judul

        Category: Manhwa
        Status: Completed
        Author: Chugong

        Synopsis
        <sinopsis dalam blok yang bisa dibuka>

        Genres
        Action • Adventure • Fantasy

        Latest Chapter: Chapter 120
        Available Language: Indonesian / English

    `languages` = kode bahasa yang BENAR-BENAR punya chapter (diverifikasi
    lewat feed, lihat ``_language_availability``). `budget` = batas karakter
    pesan: ``CAPTION_LIMIT`` bila dikirim sebagai caption foto cover,
    ``TEXT_LIMIT`` bila tidak ada cover (pesan teks biasa).
    """
    head = [f"<b>📚 {_truncate_escaped(title.title, 120)}</b>"]
    if title.indonesian_title and title.indonesian_title != title.title:
        head.append(f"Indonesian Title: "
                    f"{_truncate_escaped(title.indonesian_title, 80)}")
    if title.alt_titles:
        head.append("Alt Titles: " + _short_list(title.alt_titles, 3))

    category = title.category.capitalize() or "Unclassified"
    meta = [f"Category: {_escape(category)}",
            f"Status: {_escape(_info_status(title.status))}"]
    if title.author:
        meta.append(f"Author: {_truncate_escaped(title.author, 60)}")

    blocks = ["\n".join(head), "\n".join(meta)]
    if title.genres:
        blocks.append("Genres\n" + _short_list(title.genres, 6, sep=" • "))

    lang_names = [INFO_LANG_NAMES.get(code, code) for code in languages]
    latest_text = f"Chapter {_escape(str(latest))}" if latest else "N/A"
    language_text = " / ".join(lang_names) or "N/A"
    blocks.append(f"Latest Chapter: {latest_text}\n"
                  f"Available Language: {language_text}")
    blocks.append(f'<a href="{title.url}">Open Source</a>\nSource: MangaDex')

    synopsis = _clean_synopsis(title.synopsis_id or title.synopsis or "")
    if synopsis:
        header, open_tag, close_tag = ("Synopsis", "<blockquote expandable>",
                                       "</blockquote>")
        # sisakan ruang untuk semua blok lain + header & tag blok sinopsis
        fixed = (sum(len(block) for block in blocks) + 2 * len(blocks)
                 + len(header) + 1 + len(open_tag) + len(close_tag))
        available = budget - fixed
        if available > 40:
            block = (f"{header}\n{open_tag}"
                     f"{_truncate_escaped(synopsis, available)}{close_tag}")
            blocks.insert(2, block)  # setelah judul & info dasar

    return _truncate("\n\n".join(blocks), budget)


def _chapter_line(chapter: Chapter, releases: int = 1,
                  lang: str = mangadex.INDONESIAN_LANG) -> str:
    parts = [f'<a href="{chapter.url}">{_escape(chapter.label)}</a> {_lang_flag(lang)}']
    if chapter.title:
        parts.append(_escape(_truncate(chapter.title, 60)))
    if chapter.group:
        parts.append(f"🏛️ {_escape(_truncate(chapter.group, 40))}")
    if releases > 1:
        parts.append(f"(+{releases - 1} rilis lain)")
    return " • ".join(parts)


def group_releases(chapters: list[Chapter]) -> list[tuple[str, list[Chapter]]]:
    """Kelompokkan rilisan berdasarkan nomor chapter (urut baca).

    Mengembalikan list pasangan (kunci nomor, daftar rilisan). Chapter tanpa
    nomor dikelompokkan sebagai "oneshot".
    """
    groups: dict[str, list[Chapter]] = {}
    order: list[str] = []
    for chapter in chapters:
        key = str(chapter.number) if chapter.number else "oneshot"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(chapter)
    return [(key, groups[key]) for key in order]


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

def _search_keyboard(token: str, index: int, total: int, source_url: str,
                     label_info: str = "Manga Info") -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("📖 View Chapters", callback_data=_callback("c", token, str(index))),
        InlineKeyboardButton(f"ℹ️ {label_info}", callback_data=_callback("i", token, str(index))),
        InlineKeyboardButton("🌐 Open Source", url=source_url),
    ]]
    if index + 1 < total:
        rows.append([InlineKeyboardButton(
            "➡️ Next Results", callback_data=_callback("s", token, str(index + 1)))])
    return InlineKeyboardMarkup(rows)


def _chapters_keyboard(token: str, page: int, total_pages: int,
                       groups: list[tuple[str, list[Chapter]]],
                       source_url: str, manga_id: str) -> InlineKeyboardMarkup:
    start = page * CHAPTER_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for key, releases in groups[start:start + CHAPTER_PAGE_SIZE]:
        label = releases[0].label
        if len(releases) > 1:
            label += f" ({len(releases)} rilis)"
        action = "g" if len(releases) > 1 else "r"
        rows.append([InlineKeyboardButton(
            f"📖 {label}", callback_data=_callback(action, token, key))])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=_callback("c", token, str(page - 1))))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}",
                                    callback_data=_callback("noop", token, str(page))))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=_callback("c", token, str(page + 1))))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("🌐 Open Source", url=source_url),
        InlineKeyboardButton("🏠 Menu Manga", callback_data=_callback("menu", token, manga_id)),
    ])
    return InlineKeyboardMarkup(rows)


def _picker_keyboard(action: str, token: str, labels: list[str],
                     prefix: str = "") -> InlineKeyboardMarkup:
    rows = []
    for index, label in enumerate(labels):
        rows.append([InlineKeyboardButton(
            f"{prefix}{_truncate(label, 40)}",
            callback_data=_callback(action, token, str(index)))])
    return InlineKeyboardMarkup(rows)


def _read_keyboard(token: str, prev_number: str | None, next_number: str | None,
                   manga_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if prev_number is not None:
        nav.append(InlineKeyboardButton("⬅️ Previous Chapter",
                                        callback_data=_callback("r", token, prev_number)))
    nav.append(InlineKeyboardButton("📋 Chapters", callback_data=_callback("c", token, "0")))
    if next_number is not None:
        nav.append(InlineKeyboardButton("Next Chapter ➡️",
                                        callback_data=_callback("r", token, next_number)))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🌐 Open Source", url=manga_url(manga_id))])
    return InlineKeyboardMarkup(rows)


def _english_offer_text(query_text: str) -> str:
    """Pesan penawaran versi Inggris ketika judul tanpa chapter Indonesia."""
    return (
        f'Tidak ada chapter Bahasa Indonesia untuk "{_escape(query_text)}".\n'
        "Ingin melihat versi Bahasa Inggrisnya?\n\n"
        f"{mangadex.source_credit()}"
    )


def _english_offer_keyboard(token: str) -> InlineKeyboardMarkup:
    """Tombol konfirmasi; pencarian Inggris hanya jalan setelah "Yes, English"."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, English", callback_data=_callback("yes", token)),
        InlineKeyboardButton("No, Cancel", callback_data=_callback("no", token)),
    ]])


# ---------------------------------------------------------------------------
# Alur bantuan
# ---------------------------------------------------------------------------

def _error_text(exc: MangaDexError) -> str:
    if isinstance(exc, SourceRateLimited):
        return RATE_LIMITED_MSG
    return SOURCE_BUSY


async def _edit_message(query, text: str, markup: InlineKeyboardMarkup | None,
                        as_caption: bool = False) -> None:
    """Edit pesan hasil; kalau gagal (foto/basa-basi), kirim pesan baru."""
    try:
        if as_caption:
            await query.edit_message_caption(caption=text, parse_mode="HTML",
                                             reply_markup=markup)
        else:
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=markup,
                                          disable_web_page_preview=True)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.info("Edit pesan hasil gagal (%s); kirim pesan baru", exc)
        try:
            await query.message.reply_text(text, parse_mode="HTML",
                                           reply_markup=markup,
                                           disable_web_page_preview=True)
        except Exception:
            logger.exception("Gagal mengirim pesan pengganti")
    except Exception:
        logger.exception("Gagal mengedit pesan hasil")
        try:
            await query.message.reply_text(text, parse_mode="HTML",
                                           reply_markup=markup,
                                           disable_web_page_preview=True)
        except Exception:
            pass


async def _resolve_titles(query_text: str, category: str | None) -> list[Title]:
    """Judul dengan chapter Indonesia; kategori hanya berdasar metadata."""
    return await mangadex.find_titles_with_indonesian(query_text, category)


async def _not_found_fallback(message, query_text: str, category: str | None,
                              not_found_text: str, purpose: str = "search",
                              number: float | None = None) -> None:
    """Beda 'judul tidak ada' dengan 'judul ada tapi tanpa chapter Indonesia'.

    Pada kasus kedua, bot MENAWARKAN versi Bahasa Inggris lewat tombol
    "Yes, English" / "No, Cancel". Pencarian Inggris hanya dijalankan bila
    pengguna menekan "Yes, English" (lihat ``_start_english_from_offer``);
    tanpa konfirmasi, tidak ada chapter Inggris yang dicari atau ditampilkan.
    """
    try:
        any_titles = await mangadex.search_titles(query_text, category)
    except MangaDexError:
        await message.edit_text(SOURCE_BUSY)
        return
    if not any_titles:
        await message.edit_text(not_found_text)
        return
    token = _new_session(kind="langoffer", query=query_text, category=category,
                         purpose=purpose, number=number)
    await message.edit_text(_english_offer_text(query_text),
                            reply_markup=_english_offer_keyboard(token))


# ---------------------------------------------------------------------------
# Handler command
# ---------------------------------------------------------------------------

async def _cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      category: str | None, not_found_prefix: str) -> None:
    query_text = " ".join(context.args or []).strip()
    if not query_text:
        key = "manga" if category == "manga" else ("manhwa" if category == "manhwa" else "comic")
        await update.message.reply_text(USAGE[key])
        return

    status_msg = await update.message.reply_text("Mencari di MangaDex...")
    try:
        titles = await _resolve_titles(query_text, category)
    except MangaDexError as exc:
        logger.warning("Pencarian %r gagal: %s", query_text, exc)
        mark_failed()
        await status_msg.edit_text(_error_text(exc))
        return

    if not titles:
        await _not_found_fallback(status_msg, query_text, category,
                                  not_found_prefix.format(q=query_text),
                                  purpose="search")
        return

    token = _new_session(kind="search", query=query_text, category=category,
                         titles=titles,
                         show_category=category is None)
    await status_msg.delete()
    await _render_search_page(update.message, token, 0)


async def _render_search_page(message, token: str, index: int) -> None:
    session = _get_session(token)
    if not session:
        await message.reply_text(EXPIRED_SESSION)
        return
    titles: list[Title] = session["titles"]
    if not 0 <= index < len(titles):
        await message.reply_text("Silakan pilih salah satu hasil pencarian.")
        return
    title = titles[index]
    show_category = bool(session.get("show_category"))
    label_info = "Manhwa Info" if session.get("category") == "manhwa" else "Manga Info"
    lang = session.get("lang", mangadex.INDONESIAN_LANG)
    caption = _title_caption(title, show_category=show_category, lang=lang)
    keyboard = _search_keyboard(token, index, len(titles), title.url, label_info)

    if title.cover_url:
        try:
            await message.reply_photo(photo=title.cover_url, caption=caption,
                                      parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            logger.info("Kirim cover gagal untuk %s; fallback teks", title.id)
    await message.reply_text(caption, parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=keyboard)


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_search(update, context, "manga", 'Manga "{q}" tidak ditemukan.')


async def manhwa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_search(update, context, "manhwa", 'Manhwa "{q}" tidak ditemukan.')


async def comic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_search(update, context, None, 'Judul "{q}" tidak ditemukan.')


async def chapter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_text = " ".join(context.args or []).strip()
    if not query_text:
        await update.message.reply_text(USAGE["chapter"])
        return

    status_msg = await update.message.reply_text("Mencari judul di MangaDex...")
    try:
        titles = await _resolve_titles(query_text, None)
    except MangaDexError as exc:
        logger.warning("Pencarian chapter %r gagal: %s", query_text, exc)
        mark_failed()
        await status_msg.edit_text(_error_text(exc))
        return

    if not titles:
        await _not_found_fallback(status_msg, query_text, None,
                                  f'Judul "{query_text}" tidak ditemukan.',
                                  purpose="chapters")
        return

    if len(titles) > 1:
        token = _new_session(kind="resolve", purpose="chapters",
                             query=query_text, titles=titles)
        await status_msg.edit_text(
            PICK_A_TITLE,
            reply_markup=_picker_keyboard("p", token,
                                          [t.title for t in titles], prefix="📚 "))
        return

    await status_msg.delete()
    await _send_chapter_list(update.message, titles[0], page=0)


async def mangainfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_text = " ".join(context.args or []).strip()
    if not query_text:
        await update.message.reply_text(USAGE["mangainfo"])
        return

    status_msg = await update.message.reply_text("Mengambil info dari MangaDex...")
    try:
        candidates = (await mangadex.search_titles(query_text))[:5]
        if candidates:
            # verifikasi ketersediaan chapter Indonesia untuk kandidat teratas
            for title in candidates:
                total, latest = await mangadex.indonesian_stats(title.id)
                title.id_chapter_count = total
                title.latest_id_chapter = latest
                if total > 0:
                    break
    except MangaDexError as exc:
        logger.warning("Info %r gagal: %s", query_text, exc)
        mark_failed()
        await status_msg.edit_text(_error_text(exc))
        return

    if not candidates:
        await status_msg.edit_text(f'Judul "{query_text}" tidak ditemukan.')
        return

    if len(candidates) > 1:
        token = _new_session(kind="resolve", purpose="info",
                             query=query_text, titles=candidates)
        await status_msg.edit_text(
            PICK_A_TITLE,
            reply_markup=_picker_keyboard("p", token,
                                          [t.title for t in candidates], prefix="📚 "))
        return

    await status_msg.delete()
    await _send_info(update.message, candidates[0])


async def read_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = list(context.args or [])
    if len(args) < 2:
        await update.message.reply_text(USAGE["read"])
        return
    number_raw = args[-1].replace(",", ".")
    try:
        number = float(number_raw)
    except ValueError:
        await update.message.reply_text(USAGE["read"])
        return
    query_text = " ".join(args[:-1]).strip()

    status_msg = await update.message.reply_text("Mencari judul di MangaDex...")
    try:
        titles = await _resolve_titles(query_text, None)
    except MangaDexError as exc:
        logger.warning("Read %r gagal: %s", query_text, exc)
        mark_failed()
        await status_msg.edit_text(_error_text(exc))
        return

    if not titles:
        await _not_found_fallback(status_msg, query_text, None,
                                  f'Judul "{query_text}" tidak ditemukan.',
                                  purpose="read", number=number)
        return

    if len(titles) > 1:
        token = _new_session(kind="resolve", purpose="read",
                             query=query_text, titles=titles, number=number)
        await status_msg.edit_text(
            PICK_A_TITLE,
            reply_markup=_picker_keyboard("p", token,
                                          [t.title for t in titles], prefix="📚 "))
        return

    await status_msg.delete()
    await _start_read(update.message, titles[0], number)


async def mangafeature_cmd(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        MANGAFEATURE_HTML, parse_mode="HTML", disable_web_page_preview=True
    )


# ---------------------------------------------------------------------------
# Alur detail / chapter / baca
# ---------------------------------------------------------------------------

def _info_keyboard(title: Title) -> InlineKeyboardMarkup:
    """Tombol kartu info: buka daftar chapter + buka sumber."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Read Chapters", url=title.url),
        InlineKeyboardButton("🌐 Open Source", url=title.url),
    ]])


async def _language_availability(manga_id: str) -> dict[str, str | None]:
    """{kode bahasa: chapter terbaru} untuk bahasa yang punya chapter.

    Ketersediaan SELALU diverifikasi lewat chapter feed (metadata
    ``availableTranslatedLanguages`` terbukti basi), jadi baris
    "Available Language" tidak pernah menjanjikan bahasa yang kosong.
    Pengecekan Bahasa Indonesia meneruskan error ke pemanggil; kegagalan
    pengecekan Bahasa Inggris hanya dicatat supaya kartu info tetap tampil.
    """
    available: dict[str, str | None] = {}
    total, latest = await mangadex.language_stats(
        manga_id, mangadex.INDONESIAN_LANG)
    if total > 0:
        available[mangadex.INDONESIAN_LANG] = latest
    try:
        total, latest = await mangadex.language_stats(
            manga_id, mangadex.ENGLISH_LANG)
    except MangaDexError as exc:
        logger.info("Cek chapter Inggris untuk %s gagal: %s", manga_id, exc)
    else:
        if total > 0:
            available[mangadex.ENGLISH_LANG] = latest
    return available


async def _send_info(message, title: Title) -> None:
    try:
        available = await _language_availability(title.id)
    except MangaDexError as exc:
        if not title.id_chapter_count:
            logger.warning("Info %s gagal: %s", title.id, exc)
            mark_failed()
            await message.reply_text(_error_text(exc))
            return
        # statistik Indonesia sudah diketahui dari pencarian; info tetap tampil
        logger.info("Statistik bahasa %s gagal (%s); pakai data tersimpan",
                    title.id, exc)
        available = {mangadex.INDONESIAN_LANG: title.latest_id_chapter}

    latest = available.get(mangadex.INDONESIAN_LANG) or title.latest_id_chapter
    keyboard = _info_keyboard(title)
    if title.cover_url:
        caption = _info_text(title, latest, list(available), budget=CAPTION_LIMIT)
        try:
            await message.reply_photo(photo=title.cover_url, caption=caption,
                                      parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            logger.info("Kirim cover info gagal untuk %s; fallback teks", title.id)

    text = _info_text(title, latest, list(available), budget=TEXT_LIMIT)
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard,
                             disable_web_page_preview=True)


async def _send_chapter_list(message, title: Title, page: int,
                             lang: str = mangadex.INDONESIAN_LANG) -> None:
    try:
        chapters, truncated = await mangadex.get_chapters(title.id, lang)
    except MangaDexError as exc:
        logger.warning("Gagal mengambil chapter %s: %s", title.id, exc)
        mark_failed()
        await message.reply_text(_error_text(exc))
        return

    groups = group_releases(chapters)
    if not groups:
        await message.reply_text(
            f"{_no_chapters_text(lang)}\n\n{mangadex.source_credit()}")
        return

    total_pages = (len(groups) + CHAPTER_PAGE_SIZE - 1) // CHAPTER_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    token = _new_session(kind="chapters", title=title, lang=lang)

    start = page * CHAPTER_PAGE_SIZE
    end = start + CHAPTER_PAGE_SIZE
    lines = [
        f"<b>📖 Chapter {_lang_name(lang)} — {_escape(title.title)}</b>",
        f"{_lang_line(lang)} • Total: {len(groups)} chapter"
        + (" (daftar terpotong)" if truncated else ""),
        "",
    ]
    for offset, (key, releases) in enumerate(groups[start:end], start=start + 1):
        lines.append(f"{offset}. {_chapter_line(releases[0], len(releases), lang)}")
    lines += ["", "Source: MangaDex"]

    keyboard = _chapters_keyboard(token, page, total_pages, groups,
                                  title.url, title.id)
    await message.reply_text(_truncate("\n".join(lines), TEXT_LIMIT - 100),
                             parse_mode="HTML", reply_markup=keyboard,
                             disable_web_page_preview=True)


async def _send_release_picker(message, title: Title, number: float,
                               releases: list[Chapter],
                               lang: str = mangadex.INDONESIAN_LANG) -> None:
    labels = []
    for release in releases:
        label = release.group or "Grup tidak diketahui"
        if release.publish_at:
            label += f" • {release.publish_at[:10]}"
        labels.append(label)
    token = _new_session(kind="releases", title=title, number=number,
                         releases=releases, lang=lang)
    await message.reply_text(
        f"Pilih rilisan chapter {number:g} Bahasa {_lang_name(lang)}:",
        reply_markup=_picker_keyboard("g2", token, labels, prefix="🏛️ "))


async def _start_read(message, title: Title, number: float,
                      lang: str = mangadex.INDONESIAN_LANG) -> None:
    try:
        releases = await mangadex.get_releases(title.id, number, lang)
    except MangaDexError as exc:
        logger.warning("Mengambil rilisan ch %s/%s gagal: %s", title.id, number, exc)
        mark_failed()
        await message.reply_text(_error_text(exc))
        return

    if not releases:
        await message.reply_text(
            f"{CHAPTER_NOT_FOUND}\n\nNomor {number:g} tidak punya rilisan "
            f"Bahasa {_lang_name(lang)}.\n\n{mangadex.source_credit()}")
        return

    if len(releases) > 1:
        await _send_release_picker(message, title, number, releases, lang)
        return

    await _send_pages(message, title, releases[0], lang)


async def _neighbour_numbers(title: Title, number: float | None,
                             chapter_id: str | None = None,
                             lang: str = mangadex.INDONESIAN_LANG
                             ) -> tuple[str | None, str | None]:
    """Nomor chapter sebelum & sesudah posisi sekarang (untuk tombol navigasi)."""
    try:
        chapters, _ = await mangadex.get_chapters(title.id, lang)
    except MangaDexError:
        return None, None
    numbers: list[str] = []
    for chapter in chapters:
        key = str(chapter.number) if chapter.number else None
        if key is None:
            continue
        if key not in numbers:
            numbers.append(key)
    current_index = None
    if chapter_id:
        for index, chapter in enumerate(chapters):
            if chapter.id == chapter_id and chapter.number:
                key = str(chapter.number)
                current_index = numbers.index(key) if key in numbers else None
                break
    elif number is not None:
        for index, key in enumerate(numbers):
            try:
                if abs(float(key) - number) < 1e-6:
                    current_index = index
                    break
            except ValueError:
                continue
    if current_index is None:
        return None, None
    prev_number = numbers[current_index - 1] if current_index > 0 else None
    next_number = (numbers[current_index + 1]
                   if current_index + 1 < len(numbers) else None)
    return prev_number, next_number


async def _send_pages(message, title: Title, chapter: Chapter,
                      lang: str = mangadex.INDONESIAN_LANG) -> None:
    status_msg = await message.reply_text("Mengambil halaman chapter...")
    try:
        pages = await mangadex.get_pages(chapter.id)
    except MangaDexError as exc:
        logger.warning("At-home chapter %s gagal: %s", chapter.id, exc)
        mark_failed()
        await status_msg.edit_text(_error_text(exc))
        return

    if pages is None:
        # chapter dengan halaman kosong = chapter eksternal/tidak di-host MangaDex
        target = chapter.external_url or chapter.url
        await status_msg.edit_text(
            "Chapter ini hanya bisa dibuka langsung di sumber asli:\n" + target)
        return

    truncated = len(pages) > MAX_PAGES_SEND
    pages = pages[:MAX_PAGES_SEND]
    try:
        images = await _download_pages(pages)
    except Exception:
        logger.exception("Gagal mengunduh halaman chapter %s", chapter.id)
        images = []
    if not images:
        await status_msg.edit_text(
            f"{PAGE_LOAD_FAILED}\nBuka langsung di sumber:\n{chapter.url}")
        return

    caption_lines = [
        f"<b>{_escape(_truncate(title.title, 60))}</b> — {_escape(chapter.label)}",
    ]
    if chapter.title:
        caption_lines.append(_escape(_truncate(chapter.title, 80)))
    if chapter.group:
        caption_lines.append(f"🏛️ Grup: {_escape(_truncate(chapter.group, 50))}")
    caption_lines.append(_lang_line(lang))
    if truncated:
        caption_lines.append(f"⚠️ Menampilkan {MAX_PAGES_SEND} halaman pertama.")
    elif len(images) < len(pages):
        caption_lines.append("⚠️ Sebagian halaman tidak ikut terkirim.")
    caption_lines.append("Source: MangaDex")
    caption = _truncate("\n".join(caption_lines), CAPTION_LIMIT)

    try:
        await status_msg.edit_text("Mengirim halaman chapter...")
    except Exception:
        pass  # pesan status bisa saja sudah terhapus
    sent_ok = await _send_media_batches(message, images, caption)
    prev_number, next_number = await _neighbour_numbers(
        title, None, chapter_id=chapter.id, lang=lang)
    if sent_ok:
        keyboard = _read_keyboard(
            _new_session(kind="reader", title=title, lang=lang),
            prev_number, next_number, title.id)
        try:
            await message.reply_text(
                f"✅ {_escape(chapter.label)} selesai.",
                parse_mode="HTML", reply_markup=keyboard,
                disable_web_page_preview=True)
        except Exception:
            logger.warning("Gagal mengirim pesan navigasi baca")
    try:
        await status_msg.delete()
    except Exception:
        pass


async def _download_pages(urls: list[str]) -> list[bytes]:
    semaphore = asyncio.Semaphore(PAGE_DOWNLOAD_CONCURRENCY)
    results: list[bytes | None] = [None] * len(urls)

    async def fetch(index: int) -> None:
        async with semaphore:
            results[index] = await mangadex.download_image(urls[index])

    await asyncio.gather(*(fetch(i) for i in range(len(urls))))
    return [data for data in results if data]


async def _send_media_batches(message, images: list[bytes], caption: str) -> bool:
    batches = [images[i:i + MAX_MEDIA_GROUP]
               for i in range(0, len(images), MAX_MEDIA_GROUP)]
    sent_ok = False
    for batch_index, batch in enumerate(batches):
        media = []
        for index, data in enumerate(batch):
            cap = caption if (batch_index == 0 and index == 0) else None
            media.append(InputMediaPhoto(
                media=data, caption=cap, parse_mode="HTML" if cap else None))
        try:
            await message.reply_media_group(
                media=media, read_timeout=180, write_timeout=180)
            sent_ok = True
        except Exception:
            logger.warning("reply_media_group gagal; fallback dokumen")
            for index, data in enumerate(batch):
                cap = caption if (batch_index == 0 and index == 0) else None
                try:
                    await message.reply_document(
                        document=data, filename=f"page_{batch_index * MAX_MEDIA_GROUP + index + 1}.jpg",
                        caption=cap, parse_mode="HTML" if cap else None,
                        read_timeout=180, write_timeout=180)
                    sent_ok = True
                except Exception:
                    logger.exception("Gagal mengirim halaman chapter")
        if batch_index + 1 < len(batches):
            await asyncio.sleep(0.8)  # jaga dari flood control Telegram
    return sent_ok


# ---------------------------------------------------------------------------
# Callback inline
# ---------------------------------------------------------------------------

async def _start_english_from_offer(query, offer: dict) -> None:
    """Jalankan pencarian Bahasa Inggris — HANYA setelah "Yes, English".

    Tidak pernah dijalankan otomatis: hanya dipanggil dari aksi callback
    "yes" pada sesi penawaran (kind="langoffer"). Bila judul juga tidak
    punya chapter Inggris, pengguna diberi pesan yang jelas tanpa
    mengganti judul lain.
    """
    query_text = offer.get("query") or ""
    category = offer.get("category")
    purpose = offer.get("purpose") or "search"
    lang = mangadex.ENGLISH_LANG

    try:
        titles = await mangadex.find_titles_with_language(query_text, category, lang)
    except MangaDexError as exc:
        logger.warning("Pencarian Inggris %r gagal: %s", query_text, exc)
        await _edit_message(query, _error_text(exc), None)
        return

    if not titles:
        await _edit_message(
            query, f"{NO_EN_CHAPTERS}\n\n{mangadex.source_credit()}", None)
        return

    if purpose == "search":
        token = _new_session(kind="search", query=query_text, category=category,
                             titles=titles, lang=lang,
                             show_category=category is None)
        await _render_search_page_from_callback(query, token, 0)
        return

    if purpose in ("chapters", "read"):
        if len(titles) > 1:
            token = _new_session(kind="resolve", purpose=purpose,
                                 query=query_text, titles=titles,
                                 number=offer.get("number"), lang=lang)
            await _edit_message(
                query, PICK_A_TITLE,
                _picker_keyboard("p", token, [t.title for t in titles],
                                 prefix="📚 "))
            return
        if purpose == "chapters":
            await _edit_message(query, "Mengambil daftar chapter...", None)
            await _send_chapter_list_from_callback(query, titles[0], 0, lang)
        else:
            await _edit_message(query, "Mencari chapter...", None)
            await _start_read_from_callback(query, titles[0],
                                            float(offer.get("number")), lang)
        return

    await _edit_message(query, EXPIRED_SESSION, None)


async def manga_callback(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    parts = data.split("|")
    if len(parts) < 3 or parts[0] != "mg":
        await query.answer()
        return
    _, action, token = parts[0], parts[1], parts[2]
    extra = parts[3] if len(parts) > 3 else ""

    user = update.effective_user
    inflight_key = f"{getattr(user, 'id', 0)}:{data}"
    if inflight_key in _INFLIGHT:
        await query.answer("Masih diproses, tunggu sebentar ya.")
        return
    _INFLIGHT.add(inflight_key)
    try:
        await query.answer()
        session = _get_session(token)
        if session is None:
            await _edit_message(query, EXPIRED_SESSION, None)
            return

        if action == "noop":
            return

        if action == "yes":  # konfirmasi melihat versi Bahasa Inggris
            if session.get("kind") != "langoffer":
                await _edit_message(query, EXPIRED_SESSION, None)
                return
            SESSIONS.pop(token, None)  # sekali pakai; tolak tekan ulang
            await _edit_message(query, "Mencari chapter Bahasa Inggris...", None)
            await _start_english_from_offer(query, session)
            return

        if action == "no":  # pengguna menolak; batalkan tanpa chapter Inggris
            if session.get("kind") != "langoffer":
                await _edit_message(query, EXPIRED_SESSION, None)
                return
            SESSIONS.pop(token, None)
            await _edit_message(query, CANCELLED_ENGLISH, None)
            return

        if action == "s":  # halaman hasil pencarian berikutnya
            await _render_search_page_from_callback(query, token, int(extra))
            return

        if action in ("c", "i"):  # daftar chapter / info
            titles: list[Title] = session.get("titles") or []
            kind = session.get("kind")
            title = session.get("title")
            lang = session.get("lang", mangadex.INDONESIAN_LANG)
            page = 0
            if title is None:
                # sesi hasil pencarian / pemilih judul: extra = indeks judul
                index = int(extra or 0)
                if not 0 <= index < len(titles):
                    await _edit_message(query, PICK_A_TITLE, None)
                    return
                title = titles[index]
            elif kind in ("chapters", "reader"):
                # sesi daftar chapter: extra = nomor halaman daftar
                page = int(extra or 0)
            if action == "i":
                await _edit_message(query, "Mengambil info...", None)
                await _send_info_from_callback(query, title, lang)
            else:
                await _edit_message(query, "Mengambil daftar chapter...", None)
                await _send_chapter_list_from_callback(query, title, page, lang)
            return

        if action == "p":  # memilih judul dari daftar kandidat
            titles = session.get("titles") or []
            index = int(extra or 0)
            if not 0 <= index < len(titles):
                await _edit_message(query, PICK_A_TITLE, None)
                return
            title = titles[index]
            purpose = session.get("purpose")
            lang = session.get("lang", mangadex.INDONESIAN_LANG)
            if purpose == "chapters":
                await _edit_message(query, "Mengambil daftar chapter...", None)
                await _send_chapter_list_from_callback(query, title, 0, lang)
            elif purpose == "info":
                await _edit_message(query, "Mengambil info...", None)
                await _send_info_from_callback(query, title, lang)
            elif purpose == "read":
                await _edit_message(query, "Mencari chapter...", None)
                await _start_read_from_callback(query, title,
                                                float(session.get("number")),
                                                lang)
            else:
                await _edit_message(query, EXPIRED_SESSION, None)
            return

        if action == "g":  # chapter dengan beberapa rilisan -> pilih rilis
            title = session.get("title")
            lang = session.get("lang", mangadex.INDONESIAN_LANG)
            chapters, _ = await mangadex.get_chapters(title.id, lang)
            releases = [c for c in chapters
                        if (str(c.number) if c.number else "oneshot") == extra]
            if not releases:
                await _edit_message(query, CHAPTER_NOT_FOUND, None)
                return
            if len(releases) == 1:
                await _edit_message(query, "Mengambil halaman chapter...", None)
                await _send_pages_from_callback(query, title, releases[0])
                return
            labels = []
            for release in releases:
                label = release.group or "Grup tidak diketahui"
                if release.publish_at:
                    label += f" • {release.publish_at[:10]}"
                labels.append(label)
            token2 = _new_session(kind="releases", title=title, lang=lang,
                                  number=extra, releases=releases)
            await _edit_message(
                query,
                f"Pilih rilisan chapter {_escape(extra)} Bahasa {_lang_name(lang)}:",
                _picker_keyboard("g2", token2, labels, prefix="🏛️ "))
            return

        if action == "g2":  # rilisan terpilih -> baca
            releases = session.get("releases") or []
            index = int(extra or 0)
            if not 0 <= index < len(releases):
                await _edit_message(query, PICK_A_TITLE, None)
                return
            title = session.get("title")
            lang = session.get("lang", mangadex.INDONESIAN_LANG)
            await _edit_message(query, "Mengambil halaman chapter...", None)
            await _send_pages_from_callback(query, title, releases[index], lang)
            return

        if action == "r":  # baca nomor chapter tertentu
            title = session.get("title")
            if title is None:
                titles = session.get("titles") or []
                if titles:
                    title = titles[0]
            if title is None:
                await _edit_message(query, EXPIRED_SESSION, None)
                return
            try:
                number = float(extra)
            except ValueError:
                await _edit_message(query, CHAPTER_NOT_FOUND, None)
                return
            await _edit_message(query, "Mencari chapter...", None)
            await _start_read_from_callback(
                query, title, number,
                session.get("lang", mangadex.INDONESIAN_LANG))
            return

        if action == "menu":  # kembali ke menu utama fitur manga
            await _edit_message(query, MANGAFEATURE_HTML, None)
            return

        await _edit_message(query, EXPIRED_SESSION, None)

    except MangaDexError as exc:
        logger.warning("Callback %s gagal: %s", action, exc)
        await _edit_message(query, _error_text(exc), None)
    except Exception:
        logger.exception("Callback %s tak terduga", action)
        await _edit_message(query, "Terjadi kesalahan. Silakan coba lagi.", None)
    finally:
        _INFLIGHT.discard(inflight_key)


async def _render_search_page_from_callback(query, token: str, index: int) -> None:
    session = _get_session(token)
    if not session:
        await _edit_message(query, EXPIRED_SESSION, None)
        return
    titles: list[Title] = session.get("titles") or []
    if not 0 <= index < len(titles):
        await _edit_message(query, PICK_A_TITLE, None)
        return
    title = titles[index]
    show_category = bool(session.get("show_category"))
    label_info = "Manhwa Info" if session.get("category") == "manhwa" else "Manga Info"
    lang = session.get("lang", mangadex.INDONESIAN_LANG)
    caption = _title_caption(title, show_category=show_category, lang=lang)
    keyboard = _search_keyboard(token, index, len(titles), title.url, label_info)
    # pesan hasil bisa berupa foto; edit caption-nya kalau begitu
    is_photo = bool(getattr(query.message, "photo", None))
    await _edit_message(query, caption, keyboard, as_caption=is_photo)


async def _send_chapter_list_from_callback(query, title: Title, page: int,
                                           lang: str = mangadex.INDONESIAN_LANG) -> None:
    try:
        chapters, truncated = await mangadex.get_chapters(title.id, lang)
    except MangaDexError as exc:
        logger.warning("Callback: chapter %s gagal: %s", title.id, exc)
        await _edit_message(query, _error_text(exc), None)
        return

    groups = group_releases(chapters)
    if not groups:
        await _edit_message(query, _no_chapters_text(lang), None)
        return

    total_pages = (len(groups) + CHAPTER_PAGE_SIZE - 1) // CHAPTER_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    token = _new_session(kind="chapters", title=title, lang=lang)
    start = page * CHAPTER_PAGE_SIZE
    lines = [
        f"<b>📖 Chapter {_lang_name(lang)} — {_escape(title.title)}</b>",
        f"{_lang_line(lang)} • Total: {len(groups)} chapter"
        + (" (daftar terpotong)" if truncated else ""),
        "",
    ]
    for offset, (key, releases) in enumerate(groups[start:start + CHAPTER_PAGE_SIZE],
                                             start=start + 1):
        lines.append(f"{offset}. {_chapter_line(releases[0], len(releases), lang)}")
    lines += ["", "Source: MangaDex"]
    keyboard = _chapters_keyboard(token, page, total_pages, groups,
                                  title.url, title.id)
    await _edit_message(query, _truncate("\n".join(lines), TEXT_LIMIT - 100),
                        keyboard)


async def _send_info_from_callback(query, title: Title,
                                   lang: str = mangadex.INDONESIAN_LANG) -> None:
    try:
        available = await _language_availability(title.id)
    except MangaDexError as exc:
        logger.warning("Callback: info %s gagal: %s", title.id, exc)
        await _edit_message(query, _error_text(exc), None)
        return

    latest = available.get(lang)
    languages = list(available)
    keyboard = _info_keyboard(title)
    # pesan hasil berupa foto -> cover-nya sudah tampil, cukup ganti caption
    is_photo = bool(getattr(query.message, "photo", None))
    if is_photo:
        text = _info_text(title, latest, languages, budget=CAPTION_LIMIT)
        await _edit_message(query, text, keyboard, as_caption=True)
        return

    # pesan teks (mis. pemilih judul) -> kirim cover + info sebagai foto baru
    if title.cover_url:
        caption = _info_text(title, latest, languages, budget=CAPTION_LIMIT)
        try:
            await query.message.reply_photo(photo=title.cover_url, caption=caption,
                                            parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            logger.info("Kirim cover info gagal untuk %s; fallback teks", title.id)
        else:
            try:
                await query.message.delete()  # buang pesan pemilih/status lama
            except Exception:
                logger.info("Gagal menghapus pesan pemilih info lama")
            return

    text = _info_text(title, latest, languages, budget=TEXT_LIMIT)
    await _edit_message(query, text, keyboard)


async def _start_read_from_callback(query, title: Title, number: float,
                                    lang: str = mangadex.INDONESIAN_LANG) -> None:
    try:
        releases = await mangadex.get_releases(title.id, number, lang)
    except MangaDexError as exc:
        logger.warning("Callback: rilisan %s ch %s gagal: %s", title.id, number, exc)
        await _edit_message(query, _error_text(exc), None)
        return
    if not releases:
        await _edit_message(
            query,
            f"{CHAPTER_NOT_FOUND}\n\nNomor {number:g} tidak punya rilisan "
            f"Bahasa {_lang_name(lang)}.\n\n{mangadex.source_credit()}", None)
        return
    if len(releases) > 1:
        labels = []
        for release in releases:
            label = release.group or "Grup tidak diketahui"
            if release.publish_at:
                label += f" • {release.publish_at[:10]}"
            labels.append(label)
        token = _new_session(kind="releases", title=title, number=number,
                             releases=releases, lang=lang)
        await _edit_message(
            query,
            f"Pilih rilisan chapter {number:g} Bahasa {_lang_name(lang)}:",
            _picker_keyboard("g2", token, labels, prefix="🏛️ "))
        return
    await _edit_message(query, "Mengambil halaman chapter...", None)
    await _send_pages_from_callback(query, title, releases[0], lang)


async def _send_pages_from_callback(query, title: Title, chapter: Chapter,
                                    lang: str = mangadex.INDONESIAN_LANG) -> None:
    try:
        pages = await mangadex.get_pages(chapter.id)
    except MangaDexError as exc:
        logger.warning("Callback: at-home %s gagal: %s", chapter.id, exc)
        await _edit_message(query, _error_text(exc), None)
        return
    if pages is None:
        target = chapter.external_url or chapter.url
        await _edit_message(
            query, "Chapter ini hanya bisa dibuka langsung di sumber asli:\n" + target,
            None)
        return

    truncated = len(pages) > MAX_PAGES_SEND
    pages = pages[:MAX_PAGES_SEND]
    message = query.message
    images = await _download_pages(pages)
    if not images:
        await _edit_message(query, f"{PAGE_LOAD_FAILED}\nBuka langsung di sumber:\n{chapter.url}", None)
        return

    caption_lines = [
        f"<b>{_escape(_truncate(title.title, 60))}</b> — {_escape(chapter.label)}",
    ]
    if chapter.title:
        caption_lines.append(_escape(_truncate(chapter.title, 80)))
    if chapter.group:
        caption_lines.append(f"🏛️ Grup: {_escape(_truncate(chapter.group, 50))}")
    caption_lines.append(_lang_line(lang))
    if truncated or len(images) < len(pages):
        caption_lines.append("⚠️ Sebagian halaman tidak ikut terkirim.")
    caption_lines.append("Source: MangaDex")
    caption = _truncate("\n".join(caption_lines), CAPTION_LIMIT)

    sent_ok = await _send_media_batches(message, images, caption)
    prev_number, next_number = await _neighbour_numbers(
        title, None, chapter_id=chapter.id, lang=lang)
    if sent_ok:
        keyboard = _read_keyboard(
            _new_session(kind="reader", title=title, lang=lang),
            prev_number, next_number, title.id)
        await _edit_message(
            query, f"✅ {_escape(chapter.label)} selesai.", keyboard)
    else:
        await _edit_message(
            query, f"{PAGE_LOAD_FAILED}\nBuka langsung di sumber:\n{chapter.url}",
            None)
