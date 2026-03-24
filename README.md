# Telegram Music Downloader Bot

Production-oriented Telegram bot built with Pyrofork, `spotdl`, `yt-dlp`, FastAPI, and MongoDB. It accepts Spotify tracks and playlists, YouTube and YouTube Music URLs, and `/song` search queries. Single songs are delivered as MP3 files, while playlists are packaged as ZIP archives, uploaded to a private Telegram channel, and exposed through a simple download page.

## Features

- Accepts:
  - Spotify track URLs
  - Spotify playlist URLs
  - YouTube URLs
  - YouTube Music URLs
  - `/song <query>`
- Sends single tracks as MP3 with title, artist, duration, and thumbnail when available
- Downloads playlists, creates ZIP archives, uploads them to a private Telegram channel, and returns a web link
- Shows progress updates during download and upload stages
- Supports `/start`, `/help`, `/song`, `/myfiles`, `/cancel`
- Tracks active tasks and lets users cancel their running download
- Stores uploaded file metadata for later lookup and direct-link generation
- Uses async handlers and background-friendly subprocess execution
- Cleans temporary files after completion

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
│       │   ├── downloader.py
│       │   ├── file_utils.py
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
│       │   └── validators.py
│       └── web/
│           ├── __init__.py
│           ├── server.py
│           ├── static/
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
| `MONGODB_URI` | MongoDB connection string |
| `TUNEDROP_DOMAIN` | Your public domain (e.g. `tunedrop.example.com`) |
| `TRAEFIK_ACME_EMAIL` | Email for Let's Encrypt certificates |
| `DOWNLOAD_BASE_URL` | Must match `TUNEDROP_DOMAIN` with `https://` |

3. Build and start all services:

```bash
docker compose up -d --build
```

This starts three services:
- **TuneDrop** — the bot and web server behind Traefik
- **Traefik** — reverse proxy with automatic HTTPS (Let's Encrypt)
- **Watchtower** — checks for image updates every hour and auto-restarts containers

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

## How It Works

- Spotify tracks, playlists, and `/song` queries are downloaded through `spotdl`.
- YouTube and YouTube Music URLs are downloaded through `yt-dlp` and converted to MP3 with FFmpeg.
- Single tracks are uploaded directly to the user chat.
- Playlist ZIP files are uploaded to the configured private channel.
- Uploaded ZIP metadata, recent file history, and active task snapshots are stored in MongoDB.
- The FastAPI web server exposes:
  - a download page at `/download/{token}`
  - a file endpoint at `/file/{token}` that resolves the Telegram file path through the Bot API

## Commands

- `/start` - welcome message
- `/help` - usage guide
- `/song <name>` - search and download a song
- `/myfiles` - list recently generated playlist links
- `/cancel` - cancel current task

## Notes

- Telegram Bot API direct file URLs are not permanent. This project resolves the latest file path on demand using the stored `file_id`.
- Large playlists can take time. Use `MAX_PLAYLIST_ITEMS` to cap work.
- `spotdl` quality depends on the available source on YouTube.
- The bot cleans temporary download folders after each task, but final ZIP files remain until they are removed manually or by an external cleanup policy.
- Traefik stores Let's Encrypt certificates in the `traefik_data` Docker volume. To force a certificate renewal, delete the volume and restart: `docker compose down -v && docker compose up -d`
- Watchtower checks for updates every hour. To trigger a manual update: `docker exec watchtower watchtower tunedrop --run-once`
