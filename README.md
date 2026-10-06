# TikTok Telegram Bot

A Telegram bot that downloads TikTok videos and photo slideshows (without watermark, when supported by the source) and sends them back to the user.

## Features
- Validates TikTok URLs (including short links `vt.tiktok.com` / `vm.tiktok.com`)
- Downloads video or photo/slideshow posts via `yt-dlp`
- Sends status updates while processing
- Per-user rate limiting
- Automatic cleanup of temporary files
- Runs as a systemd service with auto-restart

## Tech Stack
- Python 3.11+
- python-telegram-bot
- yt-dlp
- python-dotenv

## File Structure
- `bot.py` — Telegram bot handlers and entry point
- `downloader.py` — yt-dlp wrapper, determines video vs. image result
- `config.py` — loads environment variables
- `utils.py` — URL validation, filename sanitization, rate limiter
- `requirements.txt` — Python dependencies
- `.env.example` — environment variable template

## Installation (clean Ubuntu Server)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg

git clone https://github.com/Zeydann/tiktok-telegram-bot.git
cd tiktok-telegram-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in your BOT_TOKEN
```

## Running Manually

```bash
source venv/bin/activate
python bot.py
```

## Running as a systemd Service

See `tiktok-bot.service` example below. After placing it in `/etc/systemd/system/`:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tiktok-bot
sudo systemctl start tiktok-bot
```

### View logs
```bash
sudo journalctl -u tiktok-bot -f
```

### Restart / Stop
```bash
sudo systemctl restart tiktok-bot
sudo systemctl stop tiktok-bot
```

## Troubleshooting
- **Download fails for most links**: update yt-dlp (`pip install -U yt-dlp`) — TikTok frequently changes its site structure.
- **Video sends fail but download succeeds**: check `MAX_FILE_SIZE` against Telegram's bot upload limit (50MB for bots without local Bot API server).
- **Bot doesn't respond at all**: verify `BOT_TOKEN` is correct and the service is running (`systemctl status tiktok-bot`).
