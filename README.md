# TuneDrop

Production-oriented Telegram music downloader bot built with Pyrofork, `spotdl`, `yt-dlp`, FFmpeg, FastAPI, and MongoDB. Accepts Spotify tracks/playlists, YouTube/YouTube Music URLs, and `/song` search queries. Single tracks are delivered as MP3 files with embedded cover art; playlists are packaged as ZIP archives, uploaded to a private Telegram channel, and exposed through a styled download page with countdown timer.

## Features

- **Input sources**: Spotify track URLs, Spotify playlists, YouTube URLs (`youtube.com`, `youtu.be`, `music.youtube.com`), `/song <query>` search
- **Single tracks**: Sent as MP3 with title, artist, duration, thumbnail, and inline download/share buttons
- **Playlists**: Downloaded, packaged as ZIP, uploaded to a private channel, and returned as a web download link
- **Song cache**: Tracks cached in a Telegram channel and MongoDB — repeated requests skip re-downloading
- **Playlist cache**: Entire playlists cached by URL — repeat requests return the download link instantly without re-processing
- **Duplicate job prevention**: Same playlist URL cannot be processed twice simultaneously per user
- **Progress system**: Single-message status editing with structured templates, FloodWait handling, 3-second edit throttle, and real download speed from yt-dlp
- **Playlist progress**: Stage-aware progress (cache check, download, package, upload) with cached/downloaded/failed counters
- **Task queue**: Concurrent task limit with queue position display and per-user cancellation
- **Rate limiting**: Per-user request throttle (10 requests/60s) prevents abuse and spam
- **Cover art**: Embedded in MP3 files from YouTube thumbnails via FFmpeg, with concurrent batch processing
- **Download page**: FastAPI-served HTML with countdown timer, file size estimate, and configurable ad slots
- **Security**: Token format validation on all web endpoints, security headers, path traversal protection
- **Commands**: `/start`, `/help`, `/song <query>`, `/myfiles`, `/cancel`
- **Admin**: `/stats` command for bot metrics
- **Cleanup**: Temporary files cleaned after each task

## Project Layout

```text
.
├── .dockerignore
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── README.md
├── requirements.txt
├── tunedrop/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   └── app/
│       ├── __init__.py
│       ├── runtime.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── client.py
│       │   ├── config.py
│       │   ├── constants.py
│       │   ├── database.py
│       │   └── logging.py
│       ├── handlers/
│       │   ├── __init__.py
│       │   ├── admin.py
│       │   ├── callback_handler.py
│       │   ├── errors.py
│       │   ├── playlist_handler.py
│       │   ├── song_command.py
│       │   ├── start.py
│       │   └── url_handler.py
│       ├── services/
│       │   ├── __init__.py
│       │   ├── cache_service.py
│       │   ├── downloader.py
│       │   ├── link_generator.py
│       │   ├── metadata.py
│       │   ├── progress.py
│       │   ├── size_estimator.py
│       │   ├── spotify_service.py
│       │   ├── uploader.py
│       │   ├── youtube_service.py
│       │   └── zip_service.py
│       ├── utils/
│       │   ├── __init__.py
│       │   ├── decorators.py
│       │   ├── ffmpeg_utils.py
│       │   ├── file_utils.py
│       │   ├── filters.py
│       │   ├── helpers.py
│       │   ├── time_utils.py
│       │   ├── ui_utils.py
│       │   └── validators.py
│       └── web/
│           ├── __init__.py
│           ├── server.py
│           ├── static/
│           │   ├── main.js
│           │   └── style.css
│           └── templates/
│               └── download.html
├── downloads/
│   ├── songs/
│   ├── playlists/
│   ├── temp/
│   └── zip/
├── data/
└── logs/
```

## Requirements

- Python 3.12 or newer
- MongoDB 6.0 or newer
- `ffmpeg` installed on the VPS
- If this server needs Cloudflare WARP, configure it on the host or VPS with `bash <(curl -fsSL git.io/warp.sh) wgd`
- Telegram bot token from BotFather
- Telegram API credentials from `my.telegram.org`
- A private Telegram channel where the bot is an admin
- Optional Spotify API credentials for better metadata resolution

## Setup

### Docker (recommended)

1. Copy `.env.example` to `.env` and fill in the values.

```bash
cp .env.example .env
```

2. At minimum, set these variables:

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID from `my.telegram.org` |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Bot token from BotFather |
| `PRIVATE_CHANNEL_ID` | Private channel ID where the bot is admin |
| `SONG_CACHE_CHANNEL_ID` | Channel for caching audio files |
| `MONGODB_URI` | MongoDB connection string |
| `TUNEDROP_DOMAIN` | Your public domain (e.g. `tunedrop.example.com`) |
| `TRAEFIK_ACME_EMAIL` | Email for Let's Encrypt certificates |
| `DOWNLOAD_BASE_URL` | Must match `TUNEDROP_DOMAIN` with `https://` |

3. Build and start all services:

```bash
docker compose up -d --build
```

This starts two services:
- **TuneDrop** — the bot and web server behind Traefik
- **Traefik** — reverse proxy with automatic HTTPS (Let's Encrypt)

4. Verify:

```bash
docker compose logs -f tunedrop
```

### Manual setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Install `ffmpeg`.

```bash
sudo apt update
sudo apt install -y ffmpeg
```

4. Optional: configure Cloudflare WARP on the host or VPS.

```bash
bash <(curl -fsSL git.io/warp.sh) wgd
```

Do not run this inside `docker build`. `warp.sh` requires host-level tools such as `systemd` and WireGuard service control.

5. Start MongoDB and copy `.env.example` to `.env`, then fill in the values.

6. Start the bot and web server (default):

```bash
python -m tunedrop
```

This runs both the Telegram bot and the web server concurrently. To run only one component:

```bash
python -m tunedrop --mode bot
python -m tunedrop --mode web
```

## Architecture

### Download Flow

1. User sends a URL or `/song` query
2. Bot resolves input type (Spotify track, Spotify playlist, YouTube track, YouTube playlist, or search)
3. For single tracks: check cache → download (spotdl or yt-dlp) → embed cover art → send MP3 or upload link
4. For playlists: check playlist cache → if cached, return instantly → otherwise check cache per track → batch download uncached tracks → embed cover art → create ZIP → upload to channel → generate download link → cache playlist for next request

### Status/Progress System

The bot uses a state-machine progress system with 10 phases:

`QUEUED` → `SEARCHING` → `CHECKING_CACHE` → `DOWNLOADING` → `CONVERTING` → `PACKAGING` → `UPLOADING` → `COMPLETED`

Error states: `FAILED`, `CANCELLED`

All status updates go through a single `DownloadTask.update()` method that:
- Enforces a 3-second minimum interval between Telegram API edits
- Handles `FloodWait` errors with adaptive backoff (up to 60s)
- Detects deleted messages and stops retrying
- Deduplicates identical status text
- Shows real download speed and ETA from yt-dlp progress hooks

### Playlist Progress Template

```
⏳ Processing playlist...

Stage: Downloading
Progress: 18/64
Cached: 7
Failed: 1
```

### Playlist Completion Template

```
✅ Playlist ready

64 tracks • 361.93 MB

Downloaded: 51
Cached: 12
Failed: 1

~5m 32s at 1024 KB/s

https://tdrp.cc/generate/xxxx
```

## Commands

- `/start` - welcome message with inline buttons
- `/help` - usage guide
- `/song <name>` - search and download a song
- `/myfiles` - list recently generated playlist links
- `/cancel` - cancel current task
- `/stats` - admin bot metrics

## Web Server

The FastAPI web server exposes:
- `GET /download/{token}` — styled download page with file info, countdown timer, and download button
- `GET /file/{token}` — streams the file from Telegram's servers with proper headers
- `GET /generate/{ref}` — resolves a persistent reference to a 24-hour expiring download link
- `GET /health` — health check endpoint

The download page features:
- Countdown timer to link expiration (24 hours)
- Visual warning state when under 1 hour remaining
- Auto-hide download button on expiration
- Responsive design with optional ad slots (configurable via `.env`)

## Caching

Songs and playlists are cached in a Telegram channel and indexed in MongoDB:
- **Spotify tracks**: keyed by Spotify track ID
- **YouTube tracks**: keyed by YouTube video ID (works with `youtube.com`, `youtu.be`, and `music.youtube.com`)
- **Search queries**: keyed by the resolved YouTube video ID after first download
- **Playlists**: keyed by source URL — repeat requests for the same playlist return the cached download link instantly
- **Playlist cache**: Entire playlists cached by source URL — repeat requests served instantly from MongoDB
- Batch cache lookups for playlists to minimize DB queries
- Failed cache sends fall back to direct delivery
- Duplicate job prevention: same playlist URL cannot be processed concurrently per user
- Duplicate job prevention: same playlist URL cannot be processed concurrently for the same user

## Security

- **Rate limiting**: 10 requests per 60-second window per user on download triggers
- **Token validation**: All web endpoints validate token format (base64url-safe) and length
- **Security headers**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`
- **Path traversal protection**: Telegram file paths are validated before use
- **Deduplication**: `once_per_message` prevents duplicate handler invocations
- **Admin-only commands**: `/stats`, `/admin`, `/ads` restricted to configured admin IDs

## Notes

- Telegram Bot API direct file URLs are not permanent. This project resolves the latest file path on demand using the stored `file_id`.
- Large playlists can take time. Use `MAX_PLAYLIST_ITEMS` to cap work.
- `spotdl` quality depends on the available source on YouTube.
- The bot cleans temporary download folders after each task, but final ZIP files remain until they are removed manually or by an external cleanup policy.
- Traefik stores Let's Encrypt certificates in the `traefik_data` Docker volume. To force a certificate renewal, delete the volume and restart: `docker compose down -v && docker compose up -d`
