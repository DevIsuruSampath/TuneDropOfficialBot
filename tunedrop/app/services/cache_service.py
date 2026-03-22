from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from tunedrop.app.core.config import settings
from tunedrop.app.core.database import get_database
from tunedrop.app.utils.validators import InputType


logger = logging.getLogger(__name__)

_SPOTIFY_ID_RE = re.compile(r"/track/([A-Za-z0-9]+)")
_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})")


def generate_cache_key(source: str, input_type: InputType, yt_info: dict[str, Any] | None = None) -> tuple[str, str] | tuple[None, None]:
    """Generate a unique cache key for a song.

    Returns (cache_key, cache_key_type) or (None, None) if no key can be generated.
    For search queries without yt_info, returns (None, None) — key is generated
    after download using the actual YouTube video ID.
    """
    if input_type == InputType.SPOTIFY_TRACK:
        match = _SPOTIFY_ID_RE.search(source)
        if match:
            return f"spotify:track:{match.group(1)}", "spotify"
        return None, None

    if input_type in (InputType.YOUTUBE_TRACK, InputType.YOUTUBE_MUSIC_TRACK):
        match = _YOUTUBE_ID_RE.search(source)
        if match:
            return f"youtube:{match.group(1)}", "youtube"
        return None, None

    # SEARCH — use YouTube video ID from yt_info if available
    if yt_info:
        yt_id = yt_info.get("id")
        if yt_id:
            return f"youtube:{yt_id}", "youtube"

    return None, None


class SongCache:
    """Manages cached songs in a private Telegram channel."""

    async def get_cached_song(self, cache_key: str) -> dict[str, Any] | None:
        """Look up a cached song by its cache key. Returns the document or None."""
        if not cache_key or not settings.song_cache_channel_id:
            return None
        db = get_database()
        doc = await db["cached_songs"].find_one({"cache_key": cache_key})
        return doc

    async def cache_song(
        self,
        cache_key: str,
        key_type: str,
        file_id: str,
        title: str,
        artist: str,
        duration: int,
        file_size: int,
        thumbnail_file_id: str | None = None,
    ) -> None:
        """Store a song's metadata and Telegram file reference in the cache."""
        if not cache_key or not settings.song_cache_channel_id:
            return
        db = get_database()
        await db["cached_songs"].update_one(
            {"cache_key": cache_key},
            {"$set": {
                "cache_key": cache_key,
                "cache_key_type": key_type,
                "title": title,
                "artist": artist,
                "duration": duration,
                "file_size": file_size,
                "telegram_file_id": file_id,
                "thumbnail_file_id": thumbnail_file_id,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        logger.info("Cached song: %s - %s (%s)", artist, title, cache_key)

    async def upload_to_cache_channel(
        self,
        app: Client,
        audio_file: Path,
        title: str,
        artist: str,
        duration: int,
        thumb_path: Path | None = None,
    ) -> tuple[str, str | None]:
        """Upload an audio file to the cache channel.

        Returns (audio_file_id, thumbnail_file_id_or_None).
        Raises RuntimeError if SONG_CACHE_CHANNEL_ID is not configured.
        """
        if not settings.song_cache_channel_id:
            raise RuntimeError("SONG_CACHE_CHANNEL_ID is not configured.")
        message: Message = await app.send_audio(
            chat_id=settings.song_cache_channel_id,
            audio=str(audio_file),
            title=title,
            performer=artist,
            duration=duration,
            thumb=str(thumb_path) if thumb_path and thumb_path.exists() else None,
        )
        audio = message.audio
        if audio is None:
            raise RuntimeError("Telegram did not return audio metadata after upload to cache channel.")
        thumbnail_file_id = None
        if message.audio and hasattr(message.audio, "thumbnail") and message.audio.thumbnail:
            thumbnail_file_id = message.audio.thumbnail.file_id
        return audio.file_id, thumbnail_file_id

    async def invalidate_cache(self, cache_key: str) -> None:
        """Remove a song from the cache."""
        if not cache_key:
            return
        db = get_database()
        result = await db["cached_songs"].delete_one({"cache_key": cache_key})
        if result.deleted_count:
            logger.info("Invalidated cache for key: %s", cache_key)


song_cache = SongCache()
