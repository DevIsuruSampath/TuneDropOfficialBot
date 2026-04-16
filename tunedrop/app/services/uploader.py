from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyrogram import Client
from pyrogram.types import Message

from tunedrop.app.core.client import get_all_clients, get_client_index, get_pyrogram_client
from tunedrop.app.core.config import settings

logger = logging.getLogger(__name__)


def _cache_key_to_source_url(cache_key: str) -> str | None:
    """Convert a cache key to a source URL for inclusion in cache channel captions."""
    if cache_key.startswith("spotify:track:"):
        track_id = cache_key.split(":")[-1]
        return f"https://open.spotify.com/track/{track_id}"
    if cache_key.startswith("youtube:"):
        video_id = cache_key.split(":")[-1]
        return f"https://www.youtube.com/watch?v={video_id}"
    return None

_FORWARD_BATCH_SIZE = 100  # Telegram forward_messages() limit per call
_FORWARD_CONCURRENCY = 3  # Parallel batch forward chunks

# Per-client upload semaphore — limits concurrent uploads per bot token
# Each token has its own Telegram DC rate limit. 2 concurrent per client = less throttling.
_PER_CLIENT_UPLOAD_LIMIT = 2
_upload_semaphores: dict[int, asyncio.Semaphore] = {}

# Round-robin counter for distributing uploads across clients
_client_counter = itertools.count()


def _get_upload_semaphore(client_id: int) -> asyncio.Semaphore:
    sem = _upload_semaphores.get(client_id)
    if sem is None:
        sem = asyncio.Semaphore(_PER_CLIENT_UPLOAD_LIMIT)
        _upload_semaphores[client_id] = sem
    return sem


def get_upload_client(preferred: Client | None = None) -> Client:
    """Get the best client for uploading.

    Distributes uploads across all available clients using round-robin.
    Each bot token has its own Telegram DC rate limit bucket, so spreading
    uploads across multiple bots avoids SaveBigFilePart throttling.
    Falls back to preferred client if no others are available.
    """
    clients = get_all_clients()
    if len(clients) <= 1:
        return preferred or clients[0]

    # Round-robin across all clients
    idx = next(_client_counter) % len(clients)
    client = clients[idx]

    # Verify client is running
    if client.is_connected:
        return client

    # Fallback to preferred or first connected client
    if preferred and preferred.is_connected:
        return preferred
    for c in clients:
        if c.is_connected:
            return c
    return preferred or clients[0]


@dataclass(slots=True)
class UploadedFile:
    message_id: int
    file_id: str
    file_name: str
    file_size: int
    bot_index: int = 0


async def upload_zip_to_storage(app: Client, zip_path: Path, caption: str, chat_id: int | None = None) -> UploadedFile:
    target_chat_id = chat_id or settings.stream_channel_id
    if not target_chat_id:
        raise RuntimeError("No storage channel configured. Set STREAM_CHANNEL_ID or pass chat_id.")
    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {zip_path}")

    upload_client = get_upload_client(app)
    bot_index = get_client_index(upload_client)
    sem = _get_upload_semaphore(id(upload_client))

    async with sem:
        message: Message = await upload_client.send_document(
            chat_id=target_chat_id,
            document=str(zip_path),
            file_name=zip_path.name,
            caption=caption,
        )
    document = message.document
    if document is None:
        raise RuntimeError("Telegram did not return document metadata.")
    return UploadedFile(
        message_id=message.id,
        file_id=document.file_id,
        file_name=document.file_name or zip_path.name,
        file_size=document.file_size or zip_path.stat().st_size,
        bot_index=bot_index,
    )


async def copy_audio_to_stream(
    app: Client,
    audio_file_id: str,
    title: str,
    artist: str,
    duration: int,
    file_size: int,
    thumb_file_id: str | None = None,
) -> UploadedFile | None:
    """Fast-copy an audio file to STREAM_CHANNEL using an existing file_id.

    No re-upload — Telegram reuses the file server-side.
    MUST use the same client that owns the file_id (file_ids are bot-specific).
    Returns None if STREAM_CHANNEL_ID is not configured or copy fails.
    """
    if not settings.stream_channel_id:
        return None

    try:
        message: Message = await app.send_audio(
            chat_id=settings.stream_channel_id,
            audio=audio_file_id,
            title=title,
            performer=artist,
            duration=duration,
            thumb=thumb_file_id,
        )
        audio = message.audio
        if audio is None:
            return None
        return UploadedFile(
            message_id=message.id,
            file_id=audio.file_id,
            file_name=f"{artist} - {title}.mp3",
            file_size=file_size,
            bot_index=get_client_index(app),
        )
    except Exception:
        logger.warning("Failed to copy audio to STREAM_CHANNEL", exc_info=True)
        return None


async def upload_audio_to_cache(
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

    Uses round-robin client selection to distribute uploads across bots.
    Returns (message_id, audio_file_id, thumbnail_file_id_or_None, bot_index).
    The caller MUST use the same bot (by bot_index) for copy_audio_to_stream,
    since file_ids are bot-specific.

    If cache_key is provided, the source URL is included in the message caption
    so that cache rebuilds can recover the proper cache key.
    """
    from tunedrop.app.core.config import settings as _settings
    target = chat_id or _settings.song_cache_channel_id
    if not target:
        raise RuntimeError("No cache channel configured. Set SONG_CACHE_CHANNEL_ID or pass chat_id.")

    upload_client = get_upload_client(app)
    bot_index = get_client_index(upload_client)
    sem = _get_upload_semaphore(id(upload_client))

    # Build caption with source URL for future rebuild recovery
    caption: str | None = None
    if cache_key:
        source_url = _cache_key_to_source_url(cache_key)
        if source_url:
            caption = f"{title} - {artist}\n{source_url}"

    async with sem:
        for attempt in range(max_retries):
            try:
                message: Message = await upload_client.send_audio(
                    chat_id=target,
                    audio=str(audio_file),
                    caption=caption,
                    title=title,
                    performer=artist,
                    duration=duration,
                    thumb=str(thumb_path) if thumb_path and thumb_path.exists() else None,
                )
                break
            except Exception as e:
                from pyrogram.errors import FloodWait as FW
                if isinstance(e, FW):
                    wait = e.value + 1
                    if attempt < max_retries - 1:
                        logger.warning("FloodWait %ds on cache upload (attempt %d/%d), sleeping %ds: %s",
                                       e.value, attempt + 1, max_retries, wait, audio_file.name)
                        await asyncio.sleep(wait)
                    else:
                        raise
                else:
                    raise

    audio = message.audio
    if audio is None:
        raise RuntimeError("Telegram did not return audio metadata after upload to cache channel.")
    thumbnail_file_id = None
    if message.audio and hasattr(message.audio, "thumbnail") and message.audio.thumbnail:
        thumbnail_file_id = message.audio.thumbnail.file_id
    return message.id, audio.file_id, thumbnail_file_id, bot_index


async def forward_cached_tracks_batch(
    app: Client,
    cached_tracks: list[dict[str, Any]],
    from_chat_id: int,
    to_chat_id: int | None = None,
) -> list[UploadedFile]:
    """Forward cached tracks from SONG_CACHE to STREAM_CHANNEL via batch forward_messages.

    Uses server-side forwarding (no disk I/O). Falls back to individual
    copy_audio_to_stream() on batch failure.

    Each cached_tracks entry must have: cache_message_id, telegram_file_id,
    title, artist, duration, file_size.
    Returns UploadedFile list in same order as cached_tracks.
    """
    forward_client = get_upload_client(app)
    forward_bot_index = get_client_index(forward_client)
    target_chat_id = to_chat_id or settings.stream_channel_id
    if not target_chat_id or not cached_tracks:
        return []

    # Collect valid message IDs preserving order
    indexed: list[tuple[int, dict[str, Any]]] = []
    for i, track in enumerate(cached_tracks):
        msg_id = track.get("cache_message_id")
        if msg_id:
            indexed.append((i, track))
    if not indexed:
        return []

    results: list[UploadedFile | None] = [None] * len(cached_tracks)

    # Build chunks
    chunks = []
    for chunk_start in range(0, len(indexed), _FORWARD_BATCH_SIZE):
        chunks.append(indexed[chunk_start:chunk_start + _FORWARD_BATCH_SIZE])

    forward_sem = asyncio.Semaphore(_FORWARD_CONCURRENCY)

    async def _forward_chunk(chunk: list[tuple[int, dict[str, Any]]]) -> None:
        async with forward_sem:
            message_ids = [track["cache_message_id"] for _, track in chunk]
            try:
                forwarded: list[Message] = await forward_client.forward_messages(
                    chat_id=target_chat_id,
                    from_chat_id=from_chat_id,
                    message_ids=message_ids,
                    drop_author=True,
                )
                if isinstance(forwarded, Message):
                    forwarded = [forwarded]
                for j, msg in enumerate(forwarded):
                    orig_idx, track = chunk[j]
                    audio = msg.audio
                    if audio:
                        results[orig_idx] = UploadedFile(
                            message_id=msg.id,
                            file_id=audio.file_id,
                            file_name=f"{track.get('artist', 'Unknown')} - {track.get('title', 'Unknown')}.mp3",
                            file_size=track.get("file_size", 0),
                            bot_index=forward_bot_index,
                        )
            except Exception:
                logger.warning("Batch forward failed, falling back to individual copies", exc_info=True)
                # Fallback: parallel individual copies
                async def _copy_one(orig_idx: int, track: dict[str, Any]) -> None:
                    stream_copy = await copy_audio_to_stream(
                        forward_client,
                        audio_file_id=track["telegram_file_id"],
                        title=track.get("title", "Unknown"),
                        artist=track.get("artist", "Unknown Artist"),
                        duration=track.get("duration", 0),
                        file_size=track.get("file_size", 0),
                    )
                    if stream_copy:
                        results[orig_idx] = stream_copy
                await asyncio.gather(
                    *[_copy_one(orig_idx, track) for orig_idx, track in chunk],
                    return_exceptions=True,
                )

    # Process all chunks in parallel (limited by semaphore)
    await asyncio.gather(*[_forward_chunk(chunk) for chunk in chunks], return_exceptions=True)

    return [r for r in results if r is not None]
