import os
import logging
import subprocess
import html as html_lib

from config import BOT_TOKEN, RATE_LIMIT_COUNT, RATE_LIMIT_WINDOW, ADMIN_ID, DOWNLOAD_DIR
from mal import search_anime, fetch_hd_image
from utils import extract_url, is_tiktok_url, is_instagram_url, is_pinterest_url, RateLimiter, ProgressReporter
from telegram import Update, InputMediaPhoto
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from downloader import download_tiktok, download_instagram, download_pinterest, DownloadError, cleanup_job

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tiktok_bot")

rate_limiter = RateLimiter(RATE_LIMIT_COUNT, RATE_LIMIT_WINDOW)

MAX_MEDIA_GROUP = 10  # Telegram's limit per media group


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a TikTok or Instagram link and I'll download the media for you."
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
        if is_tiktok:
            result = await download_tiktok(url, progress_callback=reporter.update)
        elif is_instagram:
            result = await download_instagram(url, progress_callback=reporter.update)
        else:
            result = await download_pinterest(url, progress_callback=reporter.update)
        job_dir = os.path.dirname(result.files[0])
        caption = _build_caption(result.author, result.description)

        await status_msg.edit_text("Mengirim media...")

        if result.media_type == "video":
            with open(result.files[0], "rb") as f:
                try:
                    await message.reply_video(
                        video=f, caption=caption, parse_mode="HTML",
                        read_timeout=120, write_timeout=120
                    )
                except Exception as send_err:
                    logger.warning("send_video failed, falling back to document: %s", send_err)
                    f.seek(0)
                    await message.reply_document(document=f, caption=caption, parse_mode="HTML")

        else:
            await _send_image_group(message, result.files, caption)

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

async def _send_image_group(message, files: list[str], caption: str) -> None:
    # Foto > 9MB tidak bisa dikirim sebagai photo (limit Telegram 10MB, dan
    # Telegram bisa recompress foto) — kirim sebagai document agar kualitas HD utuh.
    if any(os.path.getsize(f) > 9 * 1024 * 1024 for f in files):
        await _send_as_documents(message, files, caption)
        return

    batches = [files[i:i + MAX_MEDIA_GROUP] for i in range(0, len(files), MAX_MEDIA_GROUP)]
    opened_files = []
    try:
        for batch_index, batch in enumerate(batches):
            media = []
            for i, path in enumerate(batch):
                f = open(path, "rb")
                opened_files.append(f)
                cap = caption if (batch_index == 0 and i == 0) else None
                media.append(InputMediaPhoto(media=f, caption=cap, parse_mode="HTML" if cap else None))
            await message.reply_media_group(
                media=media, read_timeout=180, write_timeout=180, connect_timeout=30
            )
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
    app.add_handler(CommandHandler("anime", anime))
    app.add_handler(CommandHandler("restart", restart_bot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
