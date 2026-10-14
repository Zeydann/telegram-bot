import os
import shutil
import logging
import subprocess
import tempfile
import html as html_lib

from config import (
    BOT_TOKEN, RATE_LIMIT_COUNT, RATE_LIMIT_WINDOW, ADMIN_ID, DOWNLOAD_DIR,
    CONVERT_MAX_FILE_SIZE, MAX_FILE_SIZE,
)
from mal import search_anime, fetch_hd_image
from utils import extract_url, is_tiktok_url, is_instagram_url, is_pinterest_url, sanitize_filename, RateLimiter, ProgressReporter
from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from downloader import download_tiktok, download_instagram, download_pinterest, DownloadError, cleanup_job
from converter import (
    ConversionError,
    ILOVEPDF_CREDIT,
    convert_pdf_to_word,
    convert_word_to_pdf,
    inspect_pdf,
    inspect_word,
    validate_document,
)
from manga import (
    chapter_cmd,
    comic_cmd,
    mangafeature_cmd,
    manga_callback,
    manga_cmd,
    mangainfo_cmd,
    manhwa_cmd,
    read_cmd,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tiktok_bot")

rate_limiter = RateLimiter(RATE_LIMIT_COUNT, RATE_LIMIT_WINDOW)

MAX_MEDIA_GROUP = 10  # Telegram's limit per media group


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a TikTok or Instagram link and I'll download the media for you.\n\n"
        "Document converters:\n"
        "/pdf — reply to a PDF to get a .docx\n"
        "/word — reply to a Word file to get a PDF (Word → PDF conversion powered by iLovePDF)\n\n"
        "Manga & manhwa (Bahasa Indonesia):\n"
        "/manga, /manhwa, /comic, /chapter, /read, /mangainfo\n\n"
        "/feature — see all available features\n"
        "/mangafeature — see manga & manhwa commands"
    )


# Daftar fitur untuk /feature. Setiap entri harus sesuai handler yang benar-benar
# terdaftar di main() — cukup tambahkan baris di sini saat ada fitur baru.
# Struktur: (kategori, emoji, ((nama, deskripsi, command atau None), ...))
FEATURES = (
    ("Media Downloader", "📥", (
        ("TikTok Downloader", "Download video atau foto TikTok dari link yang dikirim ke bot.", None),
        ("Instagram Downloader", "Download reels dan foto Instagram dari link.", None),
        ("Pinterest Downloader", "Download video atau foto Pinterest kualitas tertinggi.", None),
    )),
    ("Info Anime", "🎌", (
        ("Anime Search", "Cari info anime di MyAnimeList beserta sinopsisnya.", "/anime"),
    )),
    ("Manga dan Manhwa", "📚", (
        ("Manga Search", "Cari manga terjemahan Indonesia di MangaDex.", "/manga"),
        ("Manhwa Search", "Cari manhwa/webtoon terjemahan Indonesia.", "/manhwa"),
        ("Comic Search", "Cari manga dan manhwa sekaligus.", "/comic"),
        ("Chapter List", "Lihat daftar chapter Bahasa Indonesia.", "/chapter"),
        ("Read Chapter", "Baca atau buka chapter Indonesia.", "/read"),
        ("Manga Info", "Lihat detail judul manga/manhwa.", "/mangainfo"),
        ("Manga Features", "Daftar command manga dan manhwa.", "/mangafeature"),
    )),
    ("Dokumen", "📄", (
        ("PDF to Word", "Konversi file PDF menjadi dokumen Word (.docx).", "/pdf"),
        ("Word to PDF", "Konversi dokumen Word (.docx) menjadi file PDF.", "/word"),
    )),
    ("Lainnya", "⚙️", (
        ("Welcome", "Pesan sambutan dan petunjuk singkat bot.", "/start"),
        ("Feature List", "Tampilkan daftar fitur bot ini.", "/feature"),
        ("Restart", "Restart layanan bot (khusus admin).", "/restart"),
    )),
)


def _build_feature_text() -> str:
    """Susun pesan /feature dalam HTML; semua teks di-escape agar aman dari parse error."""
    lines = ["<b>✨ FITUR BOT</b>", "", "Berikut fitur yang tersedia di bot ini:"]
    for category, emoji, items in FEATURES:
        lines += ["", f"<b>{emoji} {html_lib.escape(category)}</b>"]
        for name, description, command in items:
            line = f"• <b>{html_lib.escape(name)}</b> — {html_lib.escape(description)}"
            if command:
                line += f" <code>{html_lib.escape(command)}</code>"
            lines.append(line)
    lines += ["", "<i>Gunakan bot dengan mudah melalui command yang tersedia.</i>"]
    return "\n".join(lines)


async def feature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        _build_feature_text(), parse_mode="HTML", disable_web_page_preview=True
    )


async def anime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Pakai: /anime judul anime\nContoh: /anime Naruto")
        return

    status_msg = await update.message.reply_text("Mencari di MyAnimeList...")
    try:
        result = await search_anime(query)
    except Exception:
        logger.exception("MAL search failed for %s", query)
        await status_msg.edit_text("Gagal menghubungi MyAnimeList. Coba lagi nanti.")
        return

    if not result:
        await status_msg.edit_text(f'Anime "{query}" tidak ditemukan di MyAnimeList.')
        return

    # Satu pesan: foto + caption berisi info + sinopsis (limit caption 1024 karakter)
    header = (
        f"{result['title']}\n"
        f"📺 Episodes: {result['episodes']}\n"
        f"🎬 Status: {result['status']}\n"
        f"⭐ Score: {result['score']}\n"
    )
    link_line = f"MyAnimeList: {result['url']}"
    synopsis = result["synopsis"]
    # Sisa ruang di caption untuk sinopsis
    budget = 1024 - len(header) - len(link_line) - len("\n\n") - len("\nSinopsis:\n")
    if len(synopsis) > budget:
        synopsis = synopsis[: max(budget - 3, 0)].rsplit(" ", 1)[0] + "..."
        if len(synopsis) > budget:
            synopsis = synopsis[:budget]

    caption = (
        f"<b>{html_lib.escape(header)}</b>\n"
        f"Sinopsis:\n"
        f"<blockquote expandable>{html_lib.escape(synopsis)}</blockquote>\n"
        f'<a href="{result["url"]}">MyAnimeList</a>'
    )

    if result.get("image"):
        image_url = await fetch_hd_image(result["image"])
        try:
            await update.message.reply_photo(photo=image_url, caption=caption, parse_mode="HTML")
            await status_msg.delete()
        except Exception:
            logger.warning("send_photo failed, falling back to text for %s", query)
            await status_msg.edit_text(caption, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await status_msg.edit_text(caption, parse_mode="HTML", disable_web_page_preview=True)


async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or str(update.effective_user.id) != str(ADMIN_ID):
        await update.message.reply_text("Kamu tidak punya akses untuk perintah ini.")
        return
    status = await update.message.reply_text("Merestart bot...")
    logger.info("Restart requested by admin %s", update.effective_user.id)
    # Simpan chat id + message id supaya setelah restart bot bisa hapus
    # pesan "Merestart bot..." dan kirim konfirmasi
    try:
        with open(os.path.join(DOWNLOAD_DIR, ".restart_flag"), "w") as f:
            f.write(f"{update.effective_chat.id}:{status.message_id}")
    except Exception:
        pass
    # Popen supaya balasan terkirim dulu, lalu service restart
    subprocess.Popen(
        ["sudo", "-n", "systemctl", "restart", "telegram-bot"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


CONVERT_HELP = {
    "pdf": (
        "Cara pakai /pdf:\n"
        "1. Kirim file PDF ke bot ini\n"
        "2. Balas file tersebut dengan perintah /pdf\n"
        "(alternatif: kirim PDF dengan caption /pdf)\n\n"
        "Hasilnya file .docx. Catatan: PDF hasil scan tanpa teks butuh OCR, "
        "layoutnya tidak dijamin 100% sama."
    ),
    "word": (
        "Cara pakai /word:\n"
        "1. Kirim file Word (.docx/.doc) ke bot ini\n"
        "2. Balas file tersebut dengan perintah /word\n"
        "(alternatif: kirim dokumen dengan caption /word)\n\n"
        f"{ILOVEPDF_CREDIT}"
    ),
}

_converting_users: set[int] = set()  # cegah konversi dobel per user


async def pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_conversion(update, context, "pdf")


async def word_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_conversion(update, context, "word")


async def convert_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tangkap perintah /pdf atau /word yang ditulis sebagai caption dokumen."""
    message = update.message
    caption = (message.caption or "").split()
    if not caption:
        return
    command = caption[0].split("@", 1)[0].lstrip("/")
    if command in CONVERT_HELP:
        await _handle_conversion(update, context, command)


async def _handle_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    message = update.message
    if not message:
        return
    user = update.effective_user

    # Sumber file: balasan ke dokumen, atau dokumen yang membawa caption perintah
    source = None
    if message.reply_to_message and message.reply_to_message.document:
        source = message.reply_to_message
    elif message.document:
        source = message
    if source is None:
        await message.reply_text(CONVERT_HELP[command])
        return

    document = source.document
    file_name = document.file_name or ""
    try:
        ext = validate_document(file_name, document.mime_type, document.file_size, command)
    except ConversionError as e:
        await message.reply_text(str(e))
        return

    if user.id in _converting_users:
        await message.reply_text("Konversi kamu yang sebelumnya masih diproses. Tunggu sebentar ya.")
        return

    _converting_users.add(user.id)
    status_msg = await message.reply_text("Mengonversi dokumen kamu...")
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="convert_", dir=DOWNLOAD_DIR)
        src_path = os.path.join(tmp_dir, "source" + ext)
        await _download_document(context, document, src_path, user.id)

        # Validasi ulang ukuran aktual (bisa berbeda dari file_size di metadata)
        actual_size = os.path.getsize(src_path)
        if actual_size <= 0:
            raise ConversionError("File-nya kosong, tidak bisa dikonversi.")
        if actual_size > CONVERT_MAX_FILE_SIZE:
            limit = CONVERT_MAX_FILE_SIZE / (1024 * 1024)
            raise ConversionError(f"Ukuran file melebihi batas {limit:g} MB.")

        stem = sanitize_filename(os.path.splitext(os.path.basename(file_name))[0]) or "converted"
        out_name = stem + (".docx" if command == "pdf" else ".pdf")
        out_path = os.path.join(tmp_dir, out_name)

        note = None
        if command == "pdf":
            has_text = inspect_pdf(src_path)
            await convert_pdf_to_word(src_path, out_path)
            if not has_text:
                note = (
                    "Catatan: PDF ini berupa halaman scan/gambar tanpa teks, "
                    "jadi isi Word-nya mungkin tidak terbaca (perlu OCR)."
                )
        else:
            inspect_word(src_path, ext)
            await convert_word_to_pdf(src_path, out_path, document.mime_type)

        caption = ILOVEPDF_CREDIT if command == "word" else note
        await _edit_status(status_msg, "Mengirim hasil konversi...")
        with open(out_path, "rb") as fh:
            await message.reply_document(
                document=fh, filename=out_name, caption=caption,
                read_timeout=120, write_timeout=120,
            )
        try:
            await status_msg.delete()
        except Exception:
            pass  # gagal hapus pesan status tidak menggagalkan konversi
        logger.info("Conversion %s completed for user %s", command, user.id)

    except ConversionError as e:
        logger.warning("Conversion %s failed for user %s: %s", command, user.id, e)
        await _edit_status(status_msg, f"Gagal: {e}")
    except Exception:
        logger.exception("Unexpected conversion error for user %s", user.id)
        await _edit_status(status_msg, "Terjadi kesalahan tak terduga. Silakan coba lagi nanti.")
    finally:
        _converting_users.discard(user.id)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)  # bersih walau konversi gagal


async def _download_document(context, document, dest_path: str, user_id: int) -> None:
    """Unduh dokumen dari Telegram; gagal di sini bukan berarti file rusak."""
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(dest_path)
    except Exception:
        logger.warning("Document download failed for user %s", user_id)
        raise ConversionError("Gagal mengunduh dokumen dari Telegram. Coba kirim ulang file-nya.")


async def _edit_status(status_msg, text: str) -> None:
    try:
        await status_msg.edit_text(text)
    except Exception:
        pass  # pesan status bisa saja sudah terhapus


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    text = message.text or ""

    url = extract_url(text)
    if not url:
        return  # Bukan link, abaikan saja

    is_tiktok = is_tiktok_url(url)
    is_instagram = is_instagram_url(url)
    is_pinterest = is_pinterest_url(url)
    if not is_tiktok and not is_instagram and not is_pinterest:
        await message.reply_text(
            "Hanya mendukung link TikTok, Instagram, atau Pinterest (tiktok.com, vt.tiktok.com, vm.tiktok.com, instagram.com, pinterest.com)."
        )
        return

    if not rate_limiter.allow(user.id):
        retry = rate_limiter.retry_after(user.id)
        await message.reply_text(
            f"You're sending requests too fast. Please try again in {retry} seconds."
        )
        return

    platform = "TikTok" if is_tiktok else ("Instagram" if is_instagram else "Pinterest")
    logger.info("User %s requested %s URL", user.id, platform)
    status_msg = await message.reply_text("Memproses link...")

    job_dir = None
    try:
        async def edit_status(text: str):
            await status_msg.edit_text(text)

        reporter = ProgressReporter(edit_status)
        result = await _run_download(is_tiktok, is_instagram, url, reporter)
        items = result.media_items
        if not items:
            raise DownloadError("Media tidak ditemukan untuk link ini.")
        job_dir = os.path.dirname(items[0][0])
        caption = _build_caption(result.author, result.description)

        await status_msg.edit_text("Mengirim media...")

        # Kirim semua media (carousel Instagram bisa isinya
        # banyak foto + video, jadi jangan cuma file pertama).
        await _send_media(message, items, caption)

        await status_msg.delete()
        logger.info("Download completed for user %s", user.id)

    except DownloadError as e:
        logger.error("Download failed for user %s: %s", user.id, e)
        await status_msg.edit_text(f"Gagal: {e}")
    except Exception:
        logger.exception("Unexpected error handling message from user %s", user.id)
        await status_msg.edit_text("Terjadi kesalahan tak terduga. Silakan coba lagi nanti.")
    finally:
        if job_dir:
            cleanup_job(job_dir)


import html as html_lib


def _build_caption(author: str | None, description: str | None) -> str:
    lines = []
    if author:
        lines.append(f"Author: @{html_lib.escape(author)}")
    if description:
        desc = html_lib.escape(description.strip())
        lines.append(f"<blockquote expandable>{desc}</blockquote>")
    return "\n".join(lines)

async def _run_download(is_tiktok: bool, is_instagram: bool, url: str, reporter):
    """Pilih downloader sesuai platform link."""
    if is_tiktok:
        return await download_tiktok(url, progress_callback=reporter.update)
    if is_instagram:
        return await download_instagram(url, progress_callback=reporter.update)
    return await download_pinterest(url, progress_callback=reporter.update)


# Foto > 9MB tidak bisa dikirim sebagai photo (limit Telegram 10MB, dan
# Telegram bisa recompress foto) — kirim sebagai document agar kualitas HD utuh.
PHOTO_INLINE_LIMIT = 9 * 1024 * 1024


async def _send_media(message, items: list[tuple[str, str]], caption: str) -> None:
    """Kirim daftar (path, jenis) — mendukung carousel campur foto & video."""
    if len(items) == 1:
        path, kind = items[0]
        if kind == "photo":
            if os.path.getsize(path) > PHOTO_INLINE_LIMIT:
                await _send_as_documents(message, [path], caption)
            else:
                with open(path, "rb") as f:
                    await message.reply_photo(
                        photo=f, caption=caption, parse_mode="HTML",
                        read_timeout=120, write_timeout=120,
                    )
            return
        with open(path, "rb") as f:
            try:
                await message.reply_video(
                    video=f, caption=caption, parse_mode="HTML",
                    read_timeout=120, write_timeout=120
                )
            except Exception as send_err:
                logger.warning("send_video failed, falling back to document: %s", send_err)
                f.seek(0)
                await message.reply_document(document=f, caption=caption, parse_mode="HTML")
        return

    # Beberapa media → kirim sebagai media group (boleh campur foto & video,
    # urutannya tetap sama seperti di postingan).
    inline: list[tuple[str, str]] = []
    as_documents: list[str] = []
    for path, kind in items:
        size = os.path.getsize(path)
        if kind == "video" and size > MAX_FILE_SIZE:
            as_documents.append(path)
        elif kind == "photo" and size > PHOTO_INLINE_LIMIT:
            as_documents.append(path)
        else:
            inline.append((path, kind))

    await _send_as_media_group(message, inline, caption)
    if as_documents:
        # Caption sudah terkirim di media group pertama (kalau ada).
        await _send_as_documents(message, as_documents, caption if not inline else None)


async def _send_as_media_group(message, items: list[tuple[str, str]], caption: str) -> None:
    opened_files = []
    try:
        for batch_index, start in enumerate(range(0, len(items), MAX_MEDIA_GROUP)):
            batch = items[start:start + MAX_MEDIA_GROUP]
            media = []
            for i, (path, kind) in enumerate(batch):
                f = open(path, "rb")
                opened_files.append(f)
                cap = caption if (batch_index == 0 and i == 0) else None
                cls = InputMediaVideo if kind == "video" else InputMediaPhoto
                media.append(cls(media=f, caption=cap, parse_mode="HTML" if cap else None))
            try:
                await message.reply_media_group(
                    media=media, read_timeout=180, write_timeout=180, connect_timeout=30
                )
            except Exception as send_err:
                # Biasanya format video tidak didukung Telegram — kirim apa adanya.
                logger.warning("media group failed, falling back to documents: %s", send_err)
                for item in media:
                    item.media.close()
                fallback_cap = caption if batch_index == 0 else None
                await _send_as_documents(message, [p for p, _ in batch], fallback_cap)
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass


async def _send_as_documents(message, files: list[str], caption: str) -> None:
    """Kirim file apa adanya (tanpa kompresi Telegram) — kualitas tetap HD."""
    for i, path in enumerate(files):
        cap = caption if i == 0 else None
        with open(path, "rb") as f:
            await message.reply_document(
                document=f, filename=os.path.basename(path),
                caption=cap, parse_mode="HTML" if cap else None,
                read_timeout=180, write_timeout=180, connect_timeout=30,
            )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)


def main() -> None:
    request = HTTPXRequest(
        connect_timeout=15,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=15,
    )
    # Konfirmasi restart otomatis kalau sebelumnya ada /restart
    async def _on_startup(app):
        flag = os.path.join(DOWNLOAD_DIR, ".restart_flag")
        if os.path.exists(flag):
            try:
                chat_id_str, msg_id_str = open(flag).read().strip().split(":")
                chat_id, message_id = int(chat_id_str), int(msg_id_str)
                os.remove(flag)
                try:
                    await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
                except Exception:
                    pass
                await app.bot.send_message(chat_id=chat_id, text="Bot berhasil direstart ✅")
            except Exception:
                logger.exception("Failed to send restart confirmation")

    app = Application.builder().token(BOT_TOKEN).request(request).post_init(_on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("feature", feature))
    app.add_handler(CommandHandler("anime", anime))
    app.add_handler(CommandHandler("restart", restart_bot))
    app.add_handler(CommandHandler("pdf", pdf_command))
    app.add_handler(CommandHandler("word", word_command))
    # Manga & manhwa Indonesia (sumber: MangaDex)
    app.add_handler(CommandHandler("manga", manga_cmd))
    app.add_handler(CommandHandler("manhwa", manhwa_cmd))
    app.add_handler(CommandHandler("comic", comic_cmd))
    app.add_handler(CommandHandler("chapter", chapter_cmd))
    app.add_handler(CommandHandler("read", read_cmd))
    app.add_handler(CommandHandler("mangainfo", mangainfo_cmd))
    app.add_handler(CommandHandler("mangafeature", mangafeature_cmd))
    app.add_handler(
        CallbackQueryHandler(manga_callback, pattern=r"^mg\|")
    )
    # Perintah yang ditulis sebagai caption pada dokumen yang diunggah
    app.add_handler(
        MessageHandler(
            filters.Document.ALL & filters.CaptionRegex(r"^/(pdf|word)(?:@\w+)?(?:\s|$)"),
            convert_caption,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
