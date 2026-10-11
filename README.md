# Telegram Bot

A Telegram bot that downloads TikTok, Instagram & Pinterest videos and photo posts (without watermark, when supported by the source) and sends them back to the user. Built to be extensible for other platforms in the future.

## Features
- Validates TikTok, Instagram, and Pinterest URLs (including TikTok short links `vt.tiktok.com` / `vm.tiktok.com`)
- Downloads video posts via `yt-dlp`
- Downloads photo/slideshow posts via a custom scraper fallback (mobile User-Agent, since `yt-dlp` doesn't support TikTok photo posts)
- Downloads Instagram reels/posts via `yt-dlp`, with `instaloader` and `og:image` fallbacks (full-resolution photos); optional `cookies.txt` for login-required posts
- Downloads Pinterest pins at highest quality (`/originals/` upgrade); large photos are sent as documents to avoid Telegram recompression
- `/anime {title}` — searches MyAnimeList and shows the cover image, episodes, status, score, and full synopsis in one message
- `/restart` — restart the bot service (admin only, requires a passwordless sudo rule)
- Sends status updates with a live progress bar while processing
- Per-user rate limiting
- Automatic cleanup of temporary files
- Runs as a systemd service with auto-restart
- Only responds to messages containing a link (stays silent on regular chat)

## Commands
- `/start` — welcome message
- `/anime {judul}` — search anime info on MyAnimeList
- `/restart` — restart the service (only for `ADMIN_ID`)

## Tech Stack
- Python 3.11+
- python-telegram-bot
- yt-dlp
- instaloader
- httpx
- python-dotenv

## File Structure
- `bot.py` — Telegram bot handlers and entry point
- `downloader.py` — yt-dlp wrapper + custom scrapers (TikTok photos, Instagram, Pinterest)
- `mal.py` — MyAnimeList scraper for `/anime`
- `config.py` — loads environment variables
- `utils.py` — URL validation, filename sanitization, rate limiter, progress reporter
- `requirements.txt` — Python dependencies
- `.env.example` — environment variable template

## Environment Variables (`.env`)
| Key | Description |
| --- | --- |
| `BOT_TOKEN` | Token from @BotFather (required) |
| `ADMIN_ID` | Telegram user ID allowed to use `/restart` |
| `INSTAGRAM_COOKIEFILE` | Optional path to Instagram `cookies.txt` (for login-required posts) |
| `DOWNLOAD_DIR` | Temp download directory (default `/tmp/tiktok_bot`) |
| `MAX_FILE_SIZE` | Max media size in bytes (default 50MB — Telegram bot limit) |
| `RATE_LIMIT_COUNT` / `RATE_LIMIT_WINDOW` | Rate limit per user |

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

### `/restart` from Telegram (passwordless sudo)
The bot can restart itself via `/restart`. Allow it without a password:
```bash
sudo visudo -f /etc/sudoers.d/telegram-bot
```
```
zeydann ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart telegram-bot
```

## Troubleshooting
- **Download fails for most video links**: update yt-dlp (`pip install -U yt-dlp`) — TikTok frequently changes its site structure.
- **Photo/slideshow posts fail to parse**: TikTok occasionally changes the internal JSON structure of its pages. Check `_scrape_photo_post` in `downloader.py` and adjust the key path if needed.
- **Video sends fail but download succeeds**: check `MAX_FILE_SIZE` against Telegram's bot upload limit (50MB for bots without a local Bot API server).
- **Bot doesn't respond at all**: verify `BOT_TOKEN` is correct and the service is running (`systemctl status telegram-bot`).
