from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from pyrogram import Client

from tunedrop.app.core.config import settings
from tunedrop.app.core.database import get_database
from tunedrop.app.utils.memory_cache import _song_cache
from tunedrop.app.utils.validators import InputType


logger = logging.getLogger(__name__)

_SPOTIFY_ID_RE = re.compile(r"/track/([A-Za-z0-9]+)")
_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})")

# Spotify API helpers for cache rebuild key matching
_spotify_token: dict[str, Any] = {}


async def _get_spotify_token() -> str | None:
    """Get a Spotify API access token using client credentials flow."""
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        return None
    now = time.monotonic()
    if _spotify_token.get("token") and _spotify_token.get("expires_at", 0) > now + 60:
        return _spotify_token["token"]
    try:
        credentials = base64.b64encode(
            f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
        ).decode()
        req = Request(
            "https://accounts.spotify.com/api/token",
            data=urlencode({"grant_type": "client_credentials"}).encode(),
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=10))
        data = json.loads(resp.read())
        token = data["access_token"]
        _spotify_token.update({"token": token, "expires_at": now + data.get("expires_in", 3600)})
        return token
    except Exception:
        logger.warning("Failed to get Spotify API token for rebuild", exc_info=True)
    return None


async def _search_spotify_track(title: str, artist: str, token: str) -> str | None:
    """Search Spotify for a track by title and artist. Returns Spotify track ID or None."""
    query = quote(f"track:{title} artist:{artist}")
    url = f"https://api.spotify.com/v1/search?q={query}&type=track&limit=3"
    try:
        loop = asyncio.get_running_loop()
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=10))
        data = json.loads(resp.read())
        items = data.get("tracks", {}).get("items", [])
        if not items:
            return None
        # Return the first result's ID (best match)
        return items[0]["id"]
    except HTTPError as e:
        if e.code == 429:
            retry_after = int(e.headers.get("Retry-After", "1"))
            logger.info("Spotify search rate limited, waiting %ds", retry_after)
            await asyncio.sleep(retry_after)
        else:
            logger.debug("Spotify search HTTP %d for %s - %s", e.code, artist, title)
    except Exception:
        logger.debug("Spotify search failed for %s - %s", artist, title, exc_info=True)
    return None


async def _search_youtube_track(title: str, artist: str) -> str | None:
    """Search YouTube for a track and return youtube:{id} cache key or None."""
    try:
        from tunedrop.app.services.youtube_service import search_youtube_music
        info = await search_youtube_music(f"{artist} - {title}")
        if info:
            entry = info
            if info.get("entries"):
                entries = info["entries"]
                if not entries:
                    return None
                entry = entries[0]
            yt_id = entry.get("id")
            if yt_id:
                return f"youtube:{yt_id}"
    except Exception:
        logger.debug("YouTube search failed for %s - %s", artist, title, exc_info=True)
    return None


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

    if input_type == InputType.YOUTUBE_MUSIC_TRACK:
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

    def __init__(self) -> None:
        self._rebuild_lock = asyncio.Lock()
        self._rebuild_state: dict[str, Any] = self._empty_rebuild_state()

    def _empty_rebuild_state(self) -> dict[str, Any]:
        return {
            "active": False,
            "phase": "idle",
            "scanned": 0,
            "recovered": 0,
            "existing": 0,
            "matched": 0,
            "deduped": 0,
        }

    def get_rebuild_status(self) -> dict[str, Any]:
        return dict(self._rebuild_state)

    def _set_rebuild_status(self, **updates: Any) -> None:
        self._rebuild_state.update(updates)

    async def get_cached_song(self, cache_key: str) -> dict[str, Any] | None:
        """Look up a cached song by its cache key. Returns the document or None."""
        if not cache_key or not settings.song_cache_channel_id:
            return None
        # L1 memory cache check
        cached = _song_cache.get(cache_key)
        if cached is not None:
            return cached
        db = get_database()
        doc = await db["cached_songs"].find_one({"cache_key": cache_key})
        if doc:
            _song_cache.set(cache_key, doc, ttl=300.0)
        return doc

    async def get_cached_songs_batch(self, cache_keys: list[str]) -> dict[str, dict[str, Any]]:
        """Look up multiple cached songs at once. Returns {cache_key: doc}.

        Checks L1 memory cache first, only queries MongoDB for keys not in L1.
        """
        if not settings.song_cache_channel_id or not cache_keys:
            return {}
        results: dict[str, dict[str, Any]] = {}
        db_keys: list[str] = []
        for key in cache_keys:
            cached = _song_cache.get(key)
            if cached is not None:
                results[key] = cached
            else:
                db_keys.append(key)
        if db_keys:
            db = get_database()
            cursor = db["cached_songs"].find({"cache_key": {"$in": db_keys}})
            async for doc in cursor:
                key = doc.get("cache_key", "")
                if key:
                    results[key] = doc
                    _song_cache.set(key, doc, ttl=300.0)
        return results

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
        download_link: str | None = None,
        cache_message_id: int | None = None,
        flog_file_id: str | None = None,
        flog_message_id: int | None = None,
        bot_index: int = 0,
    ) -> None:
        """Store a song's metadata and Telegram file reference in the cache."""
        if not cache_key or not settings.song_cache_channel_id:
            return
        db = get_database()
        doc = {
            "cache_key": cache_key,
            "cache_key_type": key_type,
            "title": title,
            "artist": artist,
            "duration": duration,
            "file_size": file_size,
            "telegram_file_id": file_id,
            "thumbnail_file_id": thumbnail_file_id,
            "download_link": download_link,
            "created_at": datetime.now(timezone.utc),
            "bot_index": bot_index,
        }
        if cache_message_id is not None:
            doc["cache_message_id"] = cache_message_id
        if flog_file_id is not None:
            doc["flog_file_id"] = flog_file_id
        if flog_message_id is not None:
            doc["flog_message_id"] = flog_message_id
        await db["cached_songs"].update_one(
            {"cache_key": cache_key},
            {"$set": doc},
            upsert=True,
        )
        # Update L1 memory cache
        _song_cache.set(cache_key, doc, ttl=300.0)
        logger.info("Cached song: %s - %s (%s)", artist, title, cache_key)

    async def upload_to_cache_channel(
        self,
        app: Client,
        audio_file: Path,
        title: str,
        artist: str,
        duration: int,
        thumb_path: Path | None = None,
        chat_id: int | None = None,
        max_retries: int = 3,
        cache_key: str | None = None,
    ) -> tuple[int, str, str | None, int]:
        """Upload an audio file to a Telegram channel.

        Returns (message_id, audio_file_id, thumbnail_file_id_or_None, bot_index).
        Uses round-robin client selection to distribute uploads across bots.
        The caller must use bot_index to find the correct bot for copy_audio_to_stream.
        If cache_key is provided, it's included in the caption for rebuild recovery.
        """
        from tunedrop.app.services.uploader import upload_audio_to_cache
        return await upload_audio_to_cache(app, audio_file, title, artist, duration, thumb_path, chat_id, max_retries, cache_key=cache_key)

    async def cache_songs_bulk(self, docs: list[dict[str, Any]]) -> int:
        """Bulk upsert multiple cached songs in a single DB operation.

        Each doc must contain at least 'cache_key'. Returns count of upserted docs.
        """
        if not settings.song_cache_channel_id or not docs:
            return 0
        from pymongo import UpdateOne
        operations = []
        for doc in docs:
            cache_key = doc.get("cache_key")
            if not cache_key:
                continue
            operations.append(UpdateOne(
                {"cache_key": cache_key},
                {"$set": doc},
                upsert=True,
            ))
        if not operations:
            return 0
        db = get_database()
        result = await db["cached_songs"].bulk_write(operations, ordered=False)
        # Warm L1 memory cache
        for doc in docs:
            cache_key = doc.get("cache_key")
            if cache_key:
                _song_cache.set(cache_key, doc, ttl=300.0)
        count = result.upserted_count + result.modified_count
        if count:
            logger.info("Bulk cached %d songs (%d upserted, %d modified)",
                        count, result.upserted_count, result.modified_count)
        return count

    async def invalidate_cache(self, cache_key: str) -> None:
        """Remove a song from the cache."""
        if not cache_key:
            return
        _song_cache.delete(cache_key)
        db = get_database()
        result = await db["cached_songs"].delete_one({"cache_key": cache_key})
        if result.deleted_count:
            logger.info("Invalidated cache for key: %s", cache_key)

    async def rebuild_from_channel(self, client, progress_cb=None) -> dict:
        """Scan SONG_CACHE channel and rebuild missing cache entries in MongoDB.

        Returns {"scanned": N, "recovered": M, "existing": K}.
        """
        from tunedrop.app.core.config import settings as _settings
        import re

        chat_id = _settings.song_cache_channel_id
        if not chat_id:
            return {"scanned": 0, "recovered": 0, "existing": 0}

        if self._rebuild_lock.locked():
            return {"already_running": True, **self.get_rebuild_status()}

        async with self._rebuild_lock:
            self._rebuild_state = self._empty_rebuild_state()
            self._set_rebuild_status(active=True, phase="scanning")

            db = get_database()
            scanned = 0
            recovered = 0
            existing = 0
            matched = 0
            deduped = 0

            try:
                # Get all existing cache entries by message_id and cache_key for fast lookup
                existing_by_msg_id: dict[int, dict] = {}
                existing_by_cache_key: dict[str, dict] = {}
                async for doc in db["cached_songs"].find(
                    {"cache_message_id": {"$exists": True}},
                    {"cache_message_id": 1, "cache_key": 1, "_id": 0},
                ):
                    mid = doc.get("cache_message_id")
                    key = doc.get("cache_key")
                    if mid:
                        existing_by_msg_id[mid] = doc
                    if key:
                        existing_by_cache_key[key] = doc

                # Bots can't use search_messages or get_chat_history.
                # Scan message IDs in batches using get_messages (which bots CAN use).
                # Start from ID 1, scan up to 5000 in chunks of 100.
                _SCAN_BATCH = 100
                _SCAN_MAX_ID = 5000

                from pymongo.errors import DuplicateKeyError

                for batch_start in range(1, _SCAN_MAX_ID, _SCAN_BATCH):
                    message_ids = list(range(batch_start, min(batch_start + _SCAN_BATCH, _SCAN_MAX_ID + 1)))
                    messages = await client.get_messages(chat_id=chat_id, message_ids=message_ids)

                    for message in messages:
                        if message is None or message.empty:
                            continue
                        if not message.audio:
                            continue

                        scanned += 1
                        msg_id = message.id

                        # Already in cache
                        if msg_id in existing_by_msg_id:
                            existing += 1
                            continue

                        audio = message.audio
                        file_id = audio.file_id
                        title = audio.title or audio.file_name or "Unknown"
                        artist = audio.performer or "Unknown Artist"
                        duration = audio.duration or 0
                        file_size = audio.file_size or 0

                        # Try to extract Spotify/YouTube ID from caption
                        cache_key = None
                        key_type = "spotify"

                        if message.caption:
                            spotify_match = re.search(r'/track/([A-Za-z0-9]{22})', message.caption)
                            if spotify_match:
                                cache_key = f"spotify:track:{spotify_match.group(1)}"
                            yt_match = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', message.caption)
                            if yt_match and not cache_key:
                                cache_key = f"youtube:{yt_match.group(1)}"
                                key_type = "youtube"

                        # If no key from caption, use msg_id as a fallback key
                        if not cache_key:
                            cache_key = f"channel_msg:{msg_id}"
                            key_type = "channel"

                        existing_doc = existing_by_cache_key.get(cache_key)
                        if existing_doc:
                            deduped += 1
                            logger.info(
                                "Skipped duplicate cache key %s for msg %d (existing msg %s)",
                                cache_key,
                                msg_id,
                                existing_doc.get("cache_message_id"),
                            )
                            continue

                        thumb_file_id = None
                        if hasattr(audio, 'thumbnail') and audio.thumbnail:
                            thumb_file_id = audio.thumbnail.file_id

                        doc = {
                            "cache_key": cache_key,
                            "cache_key_type": key_type,
                            "title": title,
                            "artist": artist,
                            "duration": duration,
                            "file_size": file_size,
                            "telegram_file_id": file_id,
                            "thumbnail_file_id": thumb_file_id,
                            "cache_message_id": msg_id,
                            "created_at": message.date or datetime.now(timezone.utc),
                            "bot_index": 0,
                        }

                        try:
                            await db["cached_songs"].update_one(
                                {"cache_message_id": msg_id},
                                {"$set": doc},
                                upsert=True,
                            )
                        except DuplicateKeyError:
                            deduped += 1
                            existing_by_cache_key[cache_key] = {"cache_key": cache_key}
                            logger.info(
                                "Skipped duplicate cache key %s while rebuilding (msg %d)",
                                cache_key,
                                msg_id,
                            )
                            continue

                        existing_by_msg_id[msg_id] = doc
                        existing_by_cache_key[cache_key] = doc
                        _song_cache.set(cache_key, doc, ttl=300.0)
                        recovered += 1
                        logger.info("Recovered cache entry: %s - %s (msg %d)", artist, title, msg_id)

                    self._set_rebuild_status(
                        active=True,
                        phase="scanning",
                        scanned=scanned,
                        recovered=recovered,
                        existing=existing,
                        matched=matched,
                        deduped=deduped,
                    )
                    if progress_cb and scanned % 5 == 0 and scanned > 0:
                        await progress_cb(scanned, recovered, existing)

                # Phase 2: Match orphaned channel_msg keys to Spotify tracks via API
                token = await _get_spotify_token()
                if token:
                    self._set_rebuild_status(
                        active=True,
                        phase="matching",
                        scanned=scanned,
                        recovered=recovered,
                        existing=existing,
                        matched=matched,
                        deduped=deduped,
                    )
                    if progress_cb:
                        try:
                            await progress_cb(scanned, recovered, existing)
                        except Exception:
                            pass

                    orphaned_cursor = db["cached_songs"].find({"cache_key_type": "channel"})
                    orphaned_list = await orphaned_cursor.to_list(length=None)

                    for i, doc in enumerate(orphaned_list):
                        title = doc.get("title", "")
                        artist = doc.get("artist", "")
                        if not title or title == "Unknown":
                            continue

                        new_key = None
                        new_type = None

                        spotify_id = await _search_spotify_track(title, artist, token)
                        if spotify_id:
                            new_key = f"spotify:track:{spotify_id}"
                            new_type = "spotify"
                        else:
                            # Also try YouTube matching via title search
                            yt_key = await _search_youtube_track(title, artist)
                            if yt_key:
                                new_key = yt_key
                                new_type = "youtube"

                        if new_key:
                            old_key = doc.get("cache_key", "")
                            try:
                                await db["cached_songs"].update_one(
                                    {"cache_key": old_key},
                                    {"$set": {"cache_key": new_key, "cache_key_type": new_type}},
                                )
                                _song_cache.delete(old_key)
                                doc["cache_key"] = new_key
                                doc["cache_key_type"] = new_type
                                _song_cache.set(new_key, doc, ttl=300.0)
                                matched += 1
                                logger.info("Matched %s - %s → %s", artist, title, new_key)
                            except DuplicateKeyError:
                                # Target key already exists — delete the orphaned entry
                                await db["cached_songs"].delete_one({"cache_key": old_key})
                                _song_cache.delete(old_key)
                                deduped += 1
                                logger.info("Deduped %s (key %s already exists)", old_key, new_key)

                        self._set_rebuild_status(
                            active=True,
                            phase="matching",
                            scanned=scanned,
                            recovered=recovered,
                            existing=existing,
                            matched=matched,
                            deduped=deduped,
                        )
                        await asyncio.sleep(0.15)  # Rate limit Spotify API

                        if progress_cb and (i + 1) % 5 == 0:
                            try:
                                await progress_cb(scanned, recovered + matched, existing)
                            except Exception:
                                pass

                result = {
                    "scanned": scanned,
                    "recovered": recovered,
                    "existing": existing,
                    "matched": matched,
                    "deduped": deduped,
                }
                self._rebuild_state = {**result, "active": False, "phase": "completed"}
                return result
            except Exception:
                self._set_rebuild_status(active=False, phase="failed")
                raise


song_cache = SongCache()
