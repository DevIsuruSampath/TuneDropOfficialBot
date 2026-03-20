# Telegram Music Downloader Bot

Production-oriented Telegram bot built with Pyrofork, `spotdl`, `yt-dlp`, and FastAPI. It accepts Spotify tracks and playlists, YouTube and YouTube Music URLs, and `/song` search queries. Single songs are delivered as MP3 files, while playlists are packaged as ZIP archives, uploaded to a private Telegram channel, and exposed through a simple download page.

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
├── app.py
├── bot.py
├── config.py
├── requirements.txt
├── .env.example
├── README.md
├── spotify_downloader.py
├── downloads/
│   ├── songs/
│   ├── playlists/
│   ├── temp/
│   └── zip/
├── logs/
│   └── bot.log
├── data/
│   ├── users.json
│   ├── cache.json
│   └── tasks.json
├── bot/
│   ├── __init__.py
│   ├── client.py
│   ├── filters.py
│   ├── helpers.py
│   ├── messages.py
│   ├── decorators.py
│   ├── handlers/
│   ├── services/
│   └── utils/
└── web/
    ├── __init__.py
    ├── server.py
    ├── templates/
    │   └── download.html
    └── static/
        └── style.css
```

## Requirements

- Python 3.11 or newer
- `ffmpeg` installed on the VPS
- Telegram bot token from BotFather
- Telegram API credentials from `my.telegram.org`
- A private Telegram channel where the bot is an admin
- Optional Spotify API credentials for better metadata resolution

## Setup

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

4. Copy `.env.example` to `.env` and fill in the values.

5. Start both bot and web server:

```bash
python app.py
```

If you want to run only the bot:

```bash
python bot.py
```

The legacy standalone Spotify downloader was preserved as:

```bash
python spotify_downloader.py
```

## How It Works

- Spotify tracks, playlists, and `/song` queries are downloaded through `spotdl`.
- YouTube and YouTube Music URLs are downloaded through `yt-dlp` and converted to MP3 with FFmpeg.
- Single tracks are uploaded directly to the user chat.
- Playlist ZIP files are uploaded to the configured private channel.
- Uploaded ZIP metadata is saved in `data/cache.json`.
- The FastAPI app exposes:
  - a landing page at `/download/{token}`
  - a file endpoint at `/file/{token}` that resolves the Telegram file path through the Bot API

## Commands

- `/start` - welcome message
- `/help` - usage guide
- `/song <name>` - search and download a song
- `/myfiles` - list recently generated playlist links
- `/cancel` - cancel current task

## Commit Convention

Use Conventional Commits for future changes:

- `feat:` new features
- `fix:` bug fixes
- `docs:` documentation changes
- `refactor:` internal code improvements
- `chore:` maintenance work

Example:

```bash
git commit -m "feat: add telegram music downloader bot"
```

## Notes

- Telegram Bot API direct file URLs are not permanent. This project resolves the latest file path on demand using the stored `file_id`.
- Large playlists can take time. Use `MAX_PLAYLIST_ITEMS` to cap work.
- `spotdl` quality depends on the available source on YouTube.
- The bot cleans temporary download folders after each task, but final ZIP files remain until they are removed manually or by an external cleanup policy.
