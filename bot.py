import os
import logging
import subprocess

from config import BOT_TOKEN, RATE_LIMIT_COUNT, RATE_LIMIT_WINDOW, ADMIN_ID, DOWNLOAD_DIR
from utils import extract_url, is_tiktok_url, is_instagram_url, RateLimiter, ProgressReporter
from telegram import Update, InputMediaPhoto
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from downloader import download_tiktok, download_instagram, DownloadError, cleanup_job

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
    if not is_tiktok and not is_instagram:
        await message.reply_text(
            "Hanya mendukung link TikTok atau Instagram (tiktok.com, vt.tiktok.com, vm.tiktok.com, instagram.com)."
        )
        return

    if not rate_limiter.allow(user.id):
        retry = rate_limiter.retry_after(user.id)
        await message.reply_text(
            f"You're sending requests too fast. Please try again in {retry} seconds."
        )
        return

    logger.info("User %s requested %s URL", user.id, "TikTok" if is_tiktok else "Instagram")
    status_msg = await message.reply_text("Memproses link...")

    job_dir = None
    try:
        async def edit_status(text: str):
            await status_msg.edit_text(text)

        reporter = ProgressReporter(edit_status)
        if is_tiktok:
            result = await download_tiktok(url, progress_callback=reporter.update)
        else:
            result = await download_instagram(url, progress_callback=reporter.update)
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
    app.add_handler(CommandHandler("restart", restart_bot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
