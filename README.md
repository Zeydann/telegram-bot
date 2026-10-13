# Telegram Bot

A Telegram bot that downloads TikTok, Instagram & Pinterest videos and photo posts (without watermark, when supported by the source) and sends them back to the user. Built to be extensible for other platforms in the future.

## Features
- Validates TikTok, Instagram, and Pinterest URLs (including TikTok short links `vt.tiktok.com` / `vm.tiktok.com`)
- Downloads video posts via `yt-dlp`
- Downloads photo/slideshow posts via a custom scraper fallback (mobile User-Agent, since `yt-dlp` doesn't support TikTok photo posts)
- Downloads Instagram reels/posts via `yt-dlp`, with `instaloader` and `og:image` fallbacks (full-resolution photos); optional `cookies.txt` for login-required posts
- Downloads Pinterest pins at highest quality (`/originals/` upgrade); large photos are sent as documents to avoid Telegram recompression
- `/anime {title}` — searches MyAnimeList and shows the cover image, episodes, status, score, and full synopsis in one message
- `/manga`, `/manhwa`, `/comic`, `/chapter`, `/read`, `/mangainfo` — Indonesian-language manga & manhwa search and reading via the official MangaDex API (Indonesian chapters first; English chapters are only searched and shown after the user explicitly confirms via a `Yes, English` / `No, Cancel` inline button)
- `/pdf` — converts an uploaded PDF into an editable Word `.docx` (reply to the document, or send the PDF with `/pdf` as caption)
- `/word` — converts an uploaded Word `.docx`/`.doc` into a PDF (reply to the document, or send it with `/word` as caption)
- `/feature` — lists every feature that is actually implemented in the bot, grouped by category
- `/mangafeature` — lists the manga & manhwa commands
- `/restart` — restart the bot service (admin only, requires a passwordless sudo rule)
- Sends status updates with a live progress bar while processing
- Per-user rate limiting
- Automatic cleanup of temporary files
- Runs as a systemd service with auto-restart
- Only responds to messages containing a link (stays silent on regular chat)

## Commands
- `/start` — welcome message
- `/anime {judul}` — search anime info on MyAnimeList
- `/manga {judul}` — search Japanese manga with Indonesian translations
- `/manhwa {judul}` — search Korean manhwa/webtoons with Indonesian translations
- `/comic {judul}` — search both manga and manhwa
- `/chapter {judul}` — list the available Indonesian chapters
- `/read {judul} {chapter}` — read an Indonesian chapter
- `/mangainfo {judul}` — detailed information about a title
- `/mangafeature` — show the manga & manhwa command list
- `/pdf` — convert the replied PDF document to `.docx`
- `/word` — convert the replied Word document to `.pdf`
- `/feature` — show the list of available features
- `/restart` — restart the service (only for `ADMIN_ID`)

## Tech Stack
- Python 3.11+
- python-telegram-bot
- yt-dlp
- instaloader
- httpx
- python-dotenv
- pdf2docx + PyMuPDF (offline PDF → Word)
- PyJWT (iLovePDF API authentication)
- MangaDex API (public, no key required — manga/manhwa feature)

## File Structure
- `bot.py` — Telegram bot handlers and entry point
- `downloader.py` — yt-dlp wrapper + custom scrapers (TikTok photos, Instagram, Pinterest)
- `mal.py` — MyAnimeList scraper for `/anime`
- `manga.py` — manga/manhwa command handlers, inline keyboards and callback routing
- `mangadex.py` — MangaDex API provider (search, Indonesian chapter feeds, pages)
- `converter.py` — document conversion (`/pdf` locally, `/word` through the iLovePDF API)
- `config.py` — loads environment variables
- `utils.py` — URL validation, filename sanitization, rate limiter, progress reporter
- `test_conversion.py` — automated tests for the conversion feature
- `test_feature.py` — automated tests for the `/feature` command
- `test_manga.py` — automated tests for the manga/manhwa feature (no network needed)
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
| `ILOVEPDF_PUBLIC_KEY` / `ILOVEPDF_SECRET_KEY` | iLovePDF API keys — required for `/word` (empty by default, `/pdf` works without them) |
| `ILOVEPDF_REGION` | Optional processing region: `eu`, `us`, `fr`, `de`, `pl`, `in`, `sg` |
| `CONVERT_MAX_FILE_SIZE` | Max document size for conversion (default 20MB — Telegram `getFile` limit) |
| `CONVERT_TIMEOUT` | Per-request conversion timeout in seconds (default 120) |
| `CONVERT_MAX_CONCURRENT` | How many conversions may run at the same time (default 2) |

## Document Conversion (`/pdf` and `/word`)

Send a document to the bot, then reply to it with the command (or attach the document with the command as caption).

| Command | Input | Output | Engine |
| --- | --- | --- | --- |
| `/pdf` | `.pdf` | `.docx` | `pdf2docx`, runs locally on the server |
| `/word` | `.docx` / `.doc` | `.pdf` | iLovePDF REST API, tool `officepdf` |

**Workflow:** validate file (extension, MIME type, size) → download it to a temp folder →
convert → reply with the result → delete the temp folder in a `finally` block (also on failure).
A `Mengonversi dokumen kamu...` status message is sent first and removed afterwards.
Duplicate commands from a user with a conversion still running are ignored, and at most
`CONVERT_MAX_CONCURRENT` conversions run at once.

### iLovePDF setup (required for `/word`)
1. Create a free account at <https://developer.ilovepdf.com/signup>.
2. Open <https://developer.ilovepdf.com/user/projects>, create a project and copy its
   **Public Key** and **Secret Key**.
3. Put them in `.env` (never commit `.env` — it is already in `.gitignore`):
   ```bash
   ILOVEPDF_PUBLIC_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ILOVEPDF_SECRET_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```
4. Restart the bot.

Notes on quota and pricing:
- The free developer plan includes **250 files per month**; paid plans add more credits
  (<https://developer.ilovepdf.com/pricing>). One conversion = one file/credit.
- The Secret Key is only used to self-sign a short-lived JWT inside the bot. It is never
  logged and never leaves the server.
- Files are uploaded to iLovePDF's servers over SSL, and the bot also calls
  `DELETE /v1/task/{task}` after every conversion. iLovePDF additionally removes
  unprocessed files automatically after 1–2 hours.
- `/pdf` does **not** need any API key.

### Limitations (honest notes)
- The official iLovePDF REST API **does not offer a PDF → Word tool** (only Word/Excel/PPT → PDF
  via `officepdf`, plus PDF → JPG, PDF → Markdown, OCR, etc.). `/pdf` therefore runs locally with
  the open-source `pdf2docx` library instead of calling iLovePDF. No private/undocumented
  iLovePDF endpoint is scraped.
- PDF → Word keeps the layout only *approximately*. Text-based PDFs convert well; **scanned
  PDFs contain no extractable text**, so the resulting Word file may mostly contain images.
  In that case run OCR first (e.g. iLovePDF's `pdfocr` tool or `ocrmypdf`) — the bot warns you
  when it detects such a PDF.
- Legacy `.doc` input is accepted but validated by iLovePDF; `.docx` is recommended.
- Word → PDF needs working API keys, a reachable network and iLovePDF quota; without them the
  bot replies with a short setup hint instead of failing silently.
- Attribution: word → PDF conversion is powered by **iLovePDF** and is credited in the result
  caption and in `/start`. Usage follows the iLovePDF API terms.

## Manga & Manhwa (Bahasa Indonesia)

Commands: `/manga`, `/manhwa`, `/comic`, `/chapter`, `/read`, `/mangainfo`,
`/mangafeature` (also listed under `/feature`).

**Data source:** the official, public **MangaDex API** (`https://api.mangadex.org`,
docs at <https://api.mangadex.org/docs/>). No API key is required, but MangaDex's
Acceptable Usage Policy applies and is respected here:

- every result credits MangaDex (`Source: MangaDex` + website + API docs) and shows the
  scanlation group name when reading is offered,
- no ads and no paid access are built on top of the API,
- chapter images are streamed from MangaDex@Home for the requesting user only — nothing
  is re-hosted or redistributed by this bot, and every chapter offers an
  `Open Source` button linking to the original reading page,
- when a chapter has no hosted pages (e.g. externally hosted/official chapters), the bot
  sends the original link instead of images — it never bypasses access controls,
- results are restricted to `safe`/`suggestive` content ratings.

**Indonesian-first guarantee:** every chapter query passes MangaDex's
`translatedLanguage[]=id` filter and the response is filtered again client-side.
A title is only shown with Indonesian chapters when the feed confirms they exist,
and Indonesian releases are never substituted automatically. When a title has no
Indonesian release, the bot offers the English version with `Yes, English` /
`No, Cancel` inline buttons ("Tidak ada chapter Bahasa Indonesia untuk ...").
English chapters are searched and displayed only after the user presses
`Yes, English`; pressing `No, Cancel` ends the search without showing anything
in English, and the offer cannot be replayed (single-use session + in-flight
guard + normal 30-minute expiry). If no English release exists either, the bot
says so instead of substituting titles. Both language paths credit MangaDex.
Title categories (manga vs manhwa) come from the `originalLanguage` metadata
(`ja` → manga, `ko` → manhwa), never from the title text.

**Rate limiting & caching:** requests are throttled to ~4/s (below MangaDex's 5/s
limit), `429` responses honor `Retry-After`, and search/details/chapter lists are
cached in-memory (5–10 minutes) to avoid repeat lookups.

**Honest limitations:**

- MangaDex does not carry every Indonesian scanlation. Popular manhwa in particular
  often have **zero** Indonesian chapters on MangaDex (e.g. Solo Leveling) — the bot
  reports that accurately and offers the English version only with explicit consent;
  if no English release exists either, it says so instead of substituting titles.
- For very long series the chapter list is fetched up to 1,500 entries; if a title has
  more, the list is marked "daftar terpotong" (truncated) rather than pretending to be
  complete.
- Cover images are served from `uploads.mangadex.org`; if that CDN is unreachable the
  bot falls back to a text result with all metadata and links.
- Other Indonesian catalog providers were evaluated (e.g. komiku.id-based scrapers);
  they are unofficial scrapers without permitted access terms, so they are not used.

## Installation (clean Ubuntu Server)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg

git clone https://github.com/Zeydann/telegram-bot.git
cd telegram-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # also installs pdf2docx, PyMuPDF and PyJWT for /pdf and /word

cp .env.example .env
nano .env   # fill in your BOT_TOKEN (and ILOVEPDF keys to enable /word)
```

No extra system packages are needed for the conversion feature — `pdf2docx` and `PyMuPDF`
ship as wheels. `ffmpeg` stays required for video downloads.

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

## Tests

```bash
source venv/bin/activate
python test_conversion.py
python test_feature.py
python test_manga.py
```

The conversion suite covers: valid PDF → DOCX conversion, Word → PDF through a mocked iLovePDF API,
commands sent without a document (reply and caption), unsupported file types, corrupted /
password-protected / empty files, the file-size limit, iLovePDF HTTP errors (401/400/429/5xx)
and timeouts, temp-file cleanup after both successful and failed conversions, concurrent users,
duplicate commands, secret-key hygiene in logs, and Telegram handler routing.

The manga suite runs fully offline (provider calls mocked) and covers: Indonesian-first
language filtering (including mixed-language API responses), category classification from
`originalLanguage` metadata, skipping titles without Indonesian chapters, the consent-based
English fallback (offer buttons shown without any English lookup, `Yes, English` → English
search/chapters/reading with pagination and navigation, `No, Cancel` → cancel, single-use
and expired offers, duplicate presses, and the English-unavailable message), `/read`
argument parsing, release pickers for multiple scanlation groups, chapter pagination,
inline-button flows (view chapters, info, next results, previous/next chapter),
expired/invalid/duplicate callbacks, source-error and rate-limit messages, HTML escaping,
`/feature` registration and handler routing.

## Troubleshooting
- **Download fails for most video links**: update yt-dlp (`pip install -U yt-dlp`) — TikTok frequently changes its site structure.
- **Photo/slideshow posts fail to parse**: TikTok occasionally changes the internal JSON structure of its pages. Check `_scrape_photo_post` in `downloader.py` and adjust the key path if needed.
- **Video sends fail but download succeeds**: check `MAX_FILE_SIZE` against Telegram's bot upload limit (50MB for bots without a local Bot API server).
- **Bot doesn't respond at all**: verify `BOT_TOKEN` is correct and the service is running (`systemctl status telegram-bot`).
- **`/word` replies "Konversi Word → PDF belum aktif"**: `ILOVEPDF_PUBLIC_KEY` / `ILOVEPDF_SECRET_KEY` are missing in `.env` — see [Document Conversion](#document-conversion-pdf-and-word).
- **`/word` replies about invalid API keys or quota**: check the key pair at <https://developer.ilovepdf.com/user/projects> and your remaining credits.
- **`/word` replies "melebihi batas waktu"**: raise `CONVERT_TIMEOUT` in `.env` (large documents take longer).
- **Converted `.docx` looks empty**: the PDF is a scan without a text layer — OCR it first (see Limitations).
