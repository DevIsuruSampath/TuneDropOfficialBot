from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[3]
load_dotenv(BASE_DIR / ".env")


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@dataclass(slots=True)
class Settings:
    api_id: int = _safe_int(os.getenv("API_ID", ""), 0)
    api_hash: str = os.getenv("API_HASH", "")
    bot_token: str = os.getenv("BOT_TOKEN", "")
    private_channel_id: int = _safe_int(os.getenv("PRIVATE_CHANNEL_ID", ""), 0)
    song_cache_channel_id: int = _safe_int(os.getenv("SONG_CACHE_CHANNEL_ID", ""), 0)
    bot_session_name: str = os.getenv("BOT_SESSION_NAME", "music_downloader_bot")
    download_base_url: str = os.getenv(
        "DOWNLOAD_BASE_URL",
        f"https://{os.getenv('TUNEDROP_DOMAIN', '127.0.0.1:8080')}"
    )
    web_host: str = os.getenv("WEB_HOST", "0.0.0.0")
    web_port: int = _safe_int(os.getenv("WEB_PORT", "8080"), 8080)
    download_speed_kbps: float = _safe_float(os.getenv("DEFAULT_USER_SPEED_KBPS", "100"), 100.0)
    max_playlist_items: int = _safe_int(os.getenv("MAX_PLAYLIST_ITEMS", "100"), 100)
    max_concurrent_tasks: int = _safe_int(os.getenv("MAX_CONCURRENT_TASKS", "1"), 1)
    progress_update_interval: float = _safe_float(os.getenv("PROGRESS_UPDATE_INTERVAL", "2.5"), 2.5)
    spotdl_inactivity_timeout_seconds: float = _safe_float(os.getenv("SPOTDL_INACTIVITY_TIMEOUT_SECONDS", "180"), 180.0)
    auto_cleanup_minutes: int = _safe_int(os.getenv("AUTO_CLEANUP_MINUTES", "30"), 30)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    admin_user_ids: set[int] = field(
        default_factory=lambda: {
            _safe_int(value.strip(), 0)
            for value in os.getenv("ADMIN_USER_IDS", "").split(",")
            if value.strip() and _safe_int(value.strip(), 0) > 0
        }
    )
    spotify_cookie_file: str = os.getenv("SPOTDL_COOKIE_FILE", "")
    ytdlp_cookie_file: str = os.getenv("YTDLP_COOKIE_FILE", "")
    spotify_client_id: str = os.getenv("SPOTIFY_CLIENT_ID", "")
    spotify_client_secret: str = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    mongodb_uri: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    mongodb_database: str = os.getenv("MONGODB_DATABASE", "tunedrop")

    data_dir: Path = BASE_DIR / "data"
    downloads_dir: Path = BASE_DIR / "downloads"
    songs_dir: Path = downloads_dir / "songs"
    playlists_dir: Path = downloads_dir / "playlists"
    temp_dir: Path = downloads_dir / "temp"
    zip_dir: Path = downloads_dir / "zip"
    logs_dir: Path = BASE_DIR / "logs"
    log_file: Path = logs_dir / "bot.log"

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.downloads_dir,
            self.songs_dir,
            self.playlists_dir,
            self.temp_dir,
            self.zip_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        missing: list[str] = []

        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.mongodb_uri:
            missing.append("MONGODB_URI")
        if not self.mongodb_database:
            missing.append("MONGODB_DATABASE")
        if self.api_id <= 0:
            missing.append("API_ID")
        if not self.api_hash:
            missing.append("API_HASH")
        if self.private_channel_id == 0:
            missing.append("PRIVATE_CHANNEL_ID")

        if missing:
            raise RuntimeError(
                "Missing required configuration values in .env: "
                + ", ".join(missing)
            )

        if self.download_speed_kbps <= 0:
            raise RuntimeError("DEFAULT_USER_SPEED_KBPS must be greater than 0.")
        if self.max_concurrent_tasks <= 0:
            raise RuntimeError("MAX_CONCURRENT_TASKS must be greater than 0.")
        if self.max_playlist_items <= 0:
            raise RuntimeError("MAX_PLAYLIST_ITEMS must be greater than 0.")
        if self.progress_update_interval <= 0:
            raise RuntimeError("PROGRESS_UPDATE_INTERVAL must be greater than 0.")
        if self.spotdl_inactivity_timeout_seconds <= 0:
            raise RuntimeError("SPOTDL_INACTIVITY_TIMEOUT_SECONDS must be greater than 0.")


settings = Settings()
