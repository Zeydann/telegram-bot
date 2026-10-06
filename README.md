# Telegram Bot

A Telegram bot that downloads TikTok videos and photo slideshows (without watermark, when supported by the source) and sends them back to the user. Built to be extensible for other platforms in the future.

## Features
- Validates TikTok URLs (including short links `vt.tiktok.com` / `vm.tiktok.com`)
- Downloads video posts via `yt-dlp`
- Downloads photo/slideshow posts via a custom scraper fallback (mobile User-Agent, since `yt-dlp` doesn't support TikTok photo posts)
- Sends status updates while processing
- Per-user rate limiting
- Automatic cleanup of temporary files
- Runs as a systemd service with auto-restart
- Only responds to messages containing a link (stays silent on regular chat)

## Tech Stack
- Python 3.11+
- python-telegram-bot
- yt-dlp
- httpx
- python-dotenv

## File Structure
- `bot.py` — Telegram bot handlers and entry point
- `downloader.py` — yt-dlp wrapper + custom photo-post scraper fallback
- `config.py` — loads environment variables
- `utils.py` — URL validation, filename sanitization, rate limiter
- `requirements.txt` — Python dependencies
- `.env.example` — environment variable template

## Installation (clean Ubuntu Server)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg

git clone https://github.com/Zeydann/telegram-bot.git
cd telegram-bot

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

Create `/etc/systemd/system/telegram-bot.service`:

```ini
[Unit]
Description=Telegram Bot (TikTok Downloader)
After=network.target

[Service]
Type=simple
User=zeydann
WorkingDirectory=/home/zeydann/projects/telegram-bot
ExecStart=/home/zeydann/projects/telegram-bot/venv/bin/python bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

### View logs
```bash
sudo journalctl -u telegram-bot -f
```

### Restart / Stop
```bash
sudo systemctl restart telegram-bot
sudo systemctl stop telegram-bot
```

## Troubleshooting
- **Download fails for most video links**: update yt-dlp (`pip install -U yt-dlp`) — TikTok frequently changes its site structure.
- **Photo/slideshow posts fail to parse**: TikTok occasionally changes the internal JSON structure of its pages. Check `_scrape_photo_post` in `downloader.py` and adjust the key path if needed.
- **Video sends fail but download succeeds**: check `MAX_FILE_SIZE` against Telegram's bot upload limit (50MB for bots without a local Bot API server).
- **Bot doesn't respond at all**: verify `BOT_TOKEN` is correct and the service is running (`systemctl status telegram-bot`).
