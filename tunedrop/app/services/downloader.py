from __future__ import annotations

import asyncio
from collections import deque
import logging
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, Message
from yt_dlp import YoutubeDL

from tunedrop.app.core.config import settings
from tunedrop.app.services.cache_service import generate_cache_key, song_cache
from tunedrop.app.services.link_generator import link_store
from tunedrop.app.services.metadata import read_audio_metadata
from tunedrop.app.services.progress import DownloadTask
from tunedrop.app.services.uploader import upload_zip_to_storage
from tunedrop.app.services.youtube_service import _base_ytdlp_opts, extract_info
from tunedrop.app.services.zip_service import build_zip
from tunedrop.app.utils.ffmpeg_utils import extract_thumbnail_from_url
from tunedrop.app.utils.file_utils import (
    check_disk_space,
    cleanup_paths,
    ensure_clean_directory,
    find_first_file,
    list_audio_files,
    sanitize_filename,
)
from tunedrop.app.utils.time_utils import estimate_download_time
from tunedrop.app.utils.ui_utils import (
    DownloadPhase,
    build_audio_caption,
    build_audio_keyboard,
    build_large_file_message,
    build_playlist_completion,
    build_playlist_status,
    build_progress_message,
    escape_html,
)
from tunedrop.app.utils.validators import InputType


logger = logging.getLogger(__name__)

_TELEGRAM_BOT_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB
_PROGRESS_UPDATE_INTERVAL = 4.0  # seconds between Telegram message edits
_CONVERSION_TIMEOUT_BASE = 900  # minimum seconds for FFmpeg audio conversion
_MAX_AUDIO_DURATION = 3 * 3600  # 3 hours
_RESOLVE_FAILED = object()  # Sentinel: search resolution failed, not a cache hit
_cached_bot_username: str | None = None


@dataclass(slots=True)
class DownloadRequest:
    user_id: int
    chat_id: int
    source: str
    input_type: InputType

    @classmethod
    def from_search(cls, user_id: int, chat_id: int, source: str) -> "DownloadRequest":
        return cls(user_id=user_id, chat_id=chat_id, source=source, input_type=InputType.SEARCH)

    @classmethod
    def from_input(cls, user_id: int, chat_id: int, source: str, input_type: InputType) -> "DownloadRequest":
        return cls(user_id=user_id, chat_id=chat_id, source=source, input_type=input_type)


@dataclass(slots=True)
class SubprocessResult:
    recent_lines: tuple[str, ...]
    error_lines: tuple[str, ...]

    @property
    def last_error(self) -> str | None:
        return self.error_lines[-1] if self.error_lines else None

    @property
    def last_line(self) -> str | None:
        return self.recent_lines[-1] if self.recent_lines else None


class SubprocessFailure(RuntimeError):
    def __init__(self, message: str, result: SubprocessResult):
        super().__init__(message)
        self.result = result


class MusicDownloadManager:
    async def __call__(self, app: Client, message: Message, task: DownloadTask) -> None:
        request: DownloadRequest = task.request

        if request.input_type in {InputType.SPOTIFY_TRACK, InputType.SEARCH, InputType.SPOTIFY_PLAYLIST}:
            await self._handle_spotify_or_search(app, message, task)
            return

        if request.input_type in {InputType.YOUTUBE_MUSIC_TRACK, InputType.YOUTUBE_MUSIC_PLAYLIST}:
            await self._handle_youtube(app, message, task)
            return

        raise ValueError("Unsupported input type.")

    async def _handle_spotify_or_search(self, app: Client, message: Message, task: DownloadTask) -> None:
        request: DownloadRequest = task.request
        if request.input_type == InputType.SPOTIFY_PLAYLIST:
            await self._download_spotify_playlist(app, message, task)
        else:
            await self._download_spotify_track(app, message, task)

    async def _download_spotify_track(self, app: Client, message: Message, task: DownloadTask) -> None:
        # Check cache first for Spotify/YouTube URL tracks
        cache_key, cache_key_type = generate_cache_key(task.request.source, task.request.input_type)
        if cache_key:
            cached = await song_cache.get_cached_song(cache_key)
            if cached:
                try:
                    await self._send_cached_audio(app, message, cached, task)
                    await task.update(build_progress_message(DownloadPhase.COMPLETED), parse_mode=ParseMode.HTML)
                    return
                except Exception:
                    logger.warning("Cached file send failed for %s, re-downloading", cache_key)
                    await song_cache.invalidate_cache(cache_key)

        work_dir = await ensure_clean_directory(settings.temp_dir / f"{task.user_id}_{int(time.time())}")
        try:
            await task.update(build_progress_message(DownloadPhase.SEARCHING), parse_mode=ParseMode.HTML)

            spotdl_result: SubprocessResult | None = None
            if task.request.input_type != InputType.SEARCH:
                try:
                    spotdl_result = await self._run_spotdl(task, task.request.source, work_dir, playlist=False)
                except SubprocessFailure as exc:
                    spotdl_result = exc.result
                    logger.warning("spotdl failed: %s", exc)

            audio_file = find_first_file(work_dir, suffix=".mp3")
            if audio_file and audio_file.stat().st_size == 0:
                audio_file = None
            thumb_url: str | None = None
            yt_info: dict[str, Any] | None = None

            # Embed cover art in spotdl-downloaded MP3
            if audio_file and spotdl_result:
                yt_url = self._extract_youtube_url(spotdl_result)
                if yt_url:
                    yt_id_match = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", yt_url)
                    if yt_id_match:
                        thumb_url = f"https://i.ytimg.com/vi/{yt_id_match.group(1)}/maxresdefault.jpg"
                        await self._embed_cover_in_mp3(audio_file, thumb_url)

            if not audio_file:
                yt_url: str | None = None
                if spotdl_result:
                    yt_url = self._extract_youtube_url(spotdl_result)

                # For search queries, resolve to YouTube video first to check cache
                if not yt_url and task.request.input_type == InputType.SEARCH:
                    yt_url = await self._resolve_search_and_check_cache(app, message, task)
                    if yt_url is None:
                        return  # Cache hit — song already sent
                    if yt_url is _RESOLVE_FAILED:
                        yt_url = None  # Resolution failed, fall through to raw ytsearch

                if not yt_url:
                    yt_url = f"ytsearch1:{task.request.source}"
                if yt_url:
                    if task.request.input_type == InputType.SEARCH:
                        await task.update(build_progress_message(DownloadPhase.SEARCHING), parse_mode=ParseMode.HTML)
                    else:
                        await task.update(build_progress_message(DownloadPhase.SEARCHING, details="Trying alternative source..."), parse_mode=ParseMode.HTML)
                    try:
                        audio_file, thumb_url, yt_info = await self._run_ytdlp_download(task, yt_url, work_dir)
                    except Exception:
                        logger.exception("yt-dlp download failed")
                    audio_file = find_first_file(work_dir, suffix=".mp3")

            if not audio_file:
                raise RuntimeError("Could not download the track. Please try again later.")

            fallback_title = audio_file.stem
            fallback_artist = "Unknown Artist"
            if yt_info:
                fallback_title = str(yt_info.get("title") or fallback_title)
                fallback_artist = str(yt_info.get("uploader") or yt_info.get("channel") or fallback_artist)
            metadata = await read_audio_metadata(audio_file, fallback_title=fallback_title, fallback_artist=fallback_artist)
            if thumb_url:
                thumb_path = await extract_thumbnail_from_url(thumb_url, work_dir / "thumb.jpg")
                metadata.thumbnail_path = thumb_path
            file_size = audio_file.stat().st_size

            # Cache the downloaded song
            download_url: str | None = None
            cache_key, cache_key_type = generate_cache_key(task.request.source, task.request.input_type, yt_info)
            if cache_key:
                try:
                    await task.update(build_progress_message(DownloadPhase.UPLOADING), parse_mode=ParseMode.HTML)
                    audio_file_id, thumb_file_id = await song_cache.upload_to_cache_channel(
                        app, audio_file, metadata.title, metadata.artist, metadata.duration, metadata.thumbnail_path,
                    )
                    file_name = f"{metadata.artist} - {metadata.title}.mp3"
                    ref = await link_store.create_ref(
                        payload={
                            "user_id": task.user_id,
                            "chat_id": settings.song_cache_channel_id,
                            "message_id": 0,
                            "file_id": audio_file_id,
                            "file_name": file_name,
                            "file_size": file_size,
                        },
                    )
                    download_url = f"{settings.download_base_url.rstrip('/')}/generate/{ref}"
                    await song_cache.cache_song(
                        cache_key=cache_key,
                        key_type=cache_key_type,
                        file_id=audio_file_id,
                        title=metadata.title,
                        artist=metadata.artist,
                        duration=metadata.duration,
                        file_size=file_size,
                        thumbnail_file_id=thumb_file_id,
                        download_link=download_url,
                    )
                except Exception:
                    logger.exception("Failed to cache song, sending directly to user")

            await self._deliver_audio(app, message, audio_file, metadata, task, download_url=download_url)
            await task.update(build_progress_message(DownloadPhase.COMPLETED), parse_mode=ParseMode.HTML)
        finally:
            await cleanup_paths([work_dir])

    async def _download_spotify_playlist(self, app: Client, message: Message, task: DownloadTask) -> None:
        safe_name = sanitize_filename(f"spotify_playlist_{task.user_id}_{int(time.time())}")
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / safe_name)
        zip_path = settings.zip_dir / f"{safe_name}.zip"
        try:
            await task.update(build_progress_message(DownloadPhase.SEARCHING, details="Looking up playlist..."), parse_mode=ParseMode.HTML)

            # Get individual track URLs from the playlist using spotdl
            track_urls = await self._get_spotify_playlist_track_urls(task, playlist_dir)
            if not track_urls:
                raise RuntimeError("Could not retrieve playlist tracks.")

            total = len(track_urls)
            await task.update(
                build_playlist_status(DownloadPhase.CHECKING_CACHE, done=0, total=total),
                parse_mode=ParseMode.HTML,
            )

            # Separate cached and uncached tracks (batch DB query for speed)
            uncached_urls: list[str] = []
            cached_count = 0
            if settings.song_cache_channel_id:
                cache_keys_map: dict[str, str] = {}
                for url in track_urls:
                    cache_key, _ = generate_cache_key(url, InputType.SPOTIFY_TRACK)
                    if cache_key:
                        cache_keys_map[cache_key] = url
                all_cache_keys = list(cache_keys_map.keys())
                if all_cache_keys:
                    cached_map = await song_cache.get_cached_songs_batch(all_cache_keys)
                    for i, url in enumerate(track_urls, 1):
                        if task.cancelled():
                            raise asyncio.CancelledError
                        cache_key, _ = generate_cache_key(url, InputType.SPOTIFY_TRACK)
                        if cache_key and cache_key in cached_map:
                            track = await self._retrieve_cached_track(app, cached_map[cache_key], playlist_dir)
                            if track:
                                cached_count += 1
                                continue
                        uncached_urls.append(url)
                        if (cached_count + len(uncached_urls)) % 10 == 0:
                            await task.update(
                                build_playlist_status(
                                    DownloadPhase.CHECKING_CACHE,
                                    done=cached_count + len(uncached_urls),
                                    total=total,
                                    cached=cached_count,
                                ),
                                parse_mode=ParseMode.HTML,
                            )
                else:
                    uncached_urls = track_urls
            else:
                uncached_urls = track_urls

            # Download uncached tracks via spotdl (chunked batches)
            newly_downloaded: list[tuple[Path, str]] = []  # (path, spotify_url)
            failed_count = 0
            if uncached_urls:
                cached_count = total - len(uncached_urls)
                _BATCH_SIZE = 10
                remaining_urls = list(uncached_urls)

                for chunk_idx, chunk_start in enumerate(range(0, len(remaining_urls), _BATCH_SIZE)):
                    if task.cancelled():
                        raise asyncio.CancelledError
                    chunk = remaining_urls[chunk_start:chunk_start + _BATCH_SIZE]
                    done_so_far = cached_count + len(newly_downloaded)
                    batch_dir = await ensure_clean_directory(
                        settings.temp_dir / f"sp_batch_{task.user_id}_{chunk_idx}_{int(time.time())}"
                    )
                    try:
                        batch_result = await self._run_spotdl_batch(
                            task, chunk, batch_dir,
                            total_tracks=total,
                            done_offset=done_so_far,
                        )
                        downloaded_files = sorted(
                            (f for f in batch_dir.iterdir() if f.suffix.lower() == ".mp3" and f.is_file() and f.stat().st_size > 0),
                            key=lambda f: f.stat().st_ctime,
                        )
                        # Embed cover art concurrently
                        yt_urls = self._extract_youtube_urls_batch(batch_result) if batch_result else []
                        await self._embed_cover_art_batch(downloaded_files, yt_urls)
                        for i, audio_file in enumerate(downloaded_files):
                            dest = playlist_dir / audio_file.name
                            shutil.move(str(audio_file), str(dest))
                            url = chunk[i] if i < len(chunk) else None
                            if url:
                                newly_downloaded.append((dest, url))
                                # Remove from remaining so we don't re-download
                                if url in remaining_urls:
                                    remaining_urls.remove(url)
                    except SubprocessFailure as exc:
                        logger.warning("Batch chunk %d failed, salvaging completed files", chunk_idx)
                        # Salvage any files that completed before the stall
                        downloaded_files = sorted(
                            (f for f in batch_dir.iterdir() if f.suffix.lower() == ".mp3" and f.is_file() and f.stat().st_size > 0),
                            key=lambda f: f.stat().st_ctime,
                        )
                        yt_urls = self._extract_youtube_urls_batch(exc.result) if exc.result else []
                        await self._embed_cover_art_batch(downloaded_files, yt_urls)
                        for i, audio_file in enumerate(downloaded_files):
                            dest = playlist_dir / audio_file.name
                            shutil.move(str(audio_file), str(dest))
                            url = chunk[i] if i < len(chunk) else None
                            if url:
                                newly_downloaded.append((dest, url))
                                if url in remaining_urls:
                                    remaining_urls.remove(url)
                    finally:
                        await cleanup_paths([batch_dir])

                # Retry any tracks that still weren't downloaded (individual, with 1 retry)
                still_remaining = list(remaining_urls)
                for i, url in enumerate(still_remaining):
                    if task.cancelled():
                        raise asyncio.CancelledError
                    done = cached_count + len(newly_downloaded) + i + 1
                    await task.update(
                        build_playlist_status(
                            DownloadPhase.DOWNLOADING,
                            done=done,
                            total=total,
                            cached=cached_count,
                            failed=failed_count,
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                    work_dir = await ensure_clean_directory(
                        settings.temp_dir / f"sp_{task.user_id}_{len(newly_downloaded) + i}_{int(time.time())}"
                    )
                    try:
                        result = await self._run_spotdl(task, url, work_dir, playlist=False)
                        audio_file = find_first_file(work_dir, suffix=".mp3")
                        if audio_file and audio_file.stat().st_size == 0:
                            audio_file = None
                        if audio_file:
                            yt_url = self._extract_youtube_url(result)
                            await self._embed_cover_for_file(audio_file, yt_url)
                            dest = playlist_dir / audio_file.name
                            shutil.move(str(audio_file), str(dest))
                            newly_downloaded.append((dest, url))
                    except Exception:
                        logger.warning("Failed to download playlist track: %s, retrying...", url)
                        await cleanup_paths([work_dir])
                        # Retry once
                        work_dir = await ensure_clean_directory(
                            settings.temp_dir / f"sp_{task.user_id}_r{i}_{int(time.time())}"
                        )
                        try:
                            result = await self._run_spotdl(task, url, work_dir, playlist=False)
                            audio_file = find_first_file(work_dir, suffix=".mp3")
                            if audio_file and audio_file.stat().st_size == 0:
                                audio_file = None
                            if audio_file:
                                yt_url = self._extract_youtube_url(result)
                                await self._embed_cover_for_file(audio_file, yt_url)
                                dest = playlist_dir / audio_file.name
                                shutil.move(str(audio_file), str(dest))
                                newly_downloaded.append((dest, url))
                        except Exception:
                            logger.exception("Track retry also failed: %s", url)
                            failed_count += 1
                    finally:
                        await cleanup_paths([work_dir])

            # Cache newly downloaded tracks (with Spotify track ID keys)
            if newly_downloaded and settings.song_cache_channel_id:
                await self._cache_spotify_tracks(app, task, newly_downloaded)

            tracks = list_audio_files(playlist_dir)
            if not tracks:
                raise RuntimeError("Playlist download finished without MP3 files.")

            await task.update(build_progress_message(DownloadPhase.PACKAGING, details=f"{len(tracks)} tracks"), parse_mode=ParseMode.HTML)
            await build_zip(playlist_dir, zip_path)
            zip_chat_id = settings.private_channel_id or settings.song_cache_channel_id
            upload = await upload_zip_to_storage(app, zip_path, caption=f"Playlist archive for user {task.user_id}", chat_id=zip_chat_id)
            link = await link_store.create_ref(
                payload={
                    "user_id": task.user_id,
                    "chat_id": zip_chat_id,
                    "message_id": 0,
                    "file_id": upload.file_id,
                    "file_name": upload.file_name,
                    "file_size": upload.file_size,
                },
            )
            link = f"{settings.download_base_url.rstrip('/')}/generate/{link}"
            eta_seconds = estimate_download_time(upload.file_size, settings.download_speed_kbps)
            await task.update(
                build_playlist_completion(
                    track_count=len(tracks),
                    file_size=upload.file_size,
                    download_link=link,
                    estimated_time=eta_seconds,
                    speed_kbps=settings.download_speed_kbps,
                    cached_count=cached_count,
                    downloaded_count=len(newly_downloaded),
                    failed_count=failed_count,
                ),
                parse_mode=ParseMode.HTML,
            )
        finally:
            await cleanup_paths([playlist_dir, zip_path])

    async def _handle_youtube(self, app: Client, message: Message, task: DownloadTask) -> None:
        info = await extract_info(task.request.source)
        if info is None:
            raise RuntimeError("Could not retrieve video information. The URL may be invalid or the video is unavailable.")
        duration = int(info.get("duration") or 0)
        if duration > _MAX_AUDIO_DURATION:
            raise RuntimeError(f"Audio is too long ({duration // 60}m {duration % 60}s). Maximum supported length is {_MAX_AUDIO_DURATION // 60} minutes (Telegram 50MB limit).")
        entries = info.get("entries") or []
        if entries and task.request.input_type == InputType.YOUTUBE_MUSIC_PLAYLIST:
            await self._download_youtube_playlist(app, message, task, info)
        else:
            await self._download_youtube_track(app, message, task, info)

    async def _download_youtube_track(self, app: Client, message: Message, task: DownloadTask, info: dict[str, Any]) -> None:
        # Check cache first
        cache_key, cache_key_type = generate_cache_key(task.request.source, task.request.input_type, info)
        if cache_key:
            cached = await song_cache.get_cached_song(cache_key)
            if cached:
                try:
                    await self._send_cached_audio(app, message, cached, task)
                    await task.update(build_progress_message(DownloadPhase.COMPLETED), parse_mode=ParseMode.HTML)
                    return
                except Exception:
                    logger.warning("Cached file send failed for %s, re-downloading", cache_key)
                    await song_cache.invalidate_cache(cache_key)

        work_dir = await ensure_clean_directory(settings.temp_dir / f"yt_{task.user_id}_{int(time.time())}")
        thumb_path: Path | None = None
        try:
            await task.update(build_progress_message(DownloadPhase.SEARCHING), parse_mode=ParseMode.HTML)
            duration = int(info.get("duration") or 0)
            audio_file, _, _ = await self._run_ytdlp_download(task, task.request.source, work_dir, timeout=max(duration * 3 + 300, 600))
            thumb_url = info.get("thumbnail")
            if thumb_url:
                thumb_path = await extract_thumbnail_from_url(thumb_url, work_dir / "thumb.jpg")
            metadata = await read_audio_metadata(
                audio_file,
                fallback_title=str(info.get("title") or audio_file.stem),
                fallback_artist=str(info.get("uploader") or info.get("channel") or "Unknown Artist"),
            )
            metadata.thumbnail_path = thumb_path
            file_size = audio_file.stat().st_size

            # Cache the downloaded song
            download_url: str | None = None
            if cache_key:
                try:
                    await task.update(build_progress_message(DownloadPhase.UPLOADING), parse_mode=ParseMode.HTML)
                    audio_file_id, thumb_file_id = await song_cache.upload_to_cache_channel(
                        app, audio_file, metadata.title, metadata.artist, metadata.duration, thumb_path,
                    )
                    file_name = f"{metadata.artist} - {metadata.title}.mp3"
                    ref = await link_store.create_ref(
                        payload={
                            "user_id": task.user_id,
                            "chat_id": settings.song_cache_channel_id,
                            "message_id": 0,
                            "file_id": audio_file_id,
                            "file_name": file_name,
                            "file_size": file_size,
                        },
                    )
                    download_url = f"{settings.download_base_url.rstrip('/')}/generate/{ref}"
                    await song_cache.cache_song(
                        cache_key=cache_key,
                        key_type=cache_key_type,
                        file_id=audio_file_id,
                        title=metadata.title,
                        artist=metadata.artist,
                        duration=metadata.duration,
                        file_size=file_size,
                        thumbnail_file_id=thumb_file_id,
                        download_link=download_url,
                    )
                except Exception:
                    logger.exception("Failed to cache song, sending directly to user")

            await self._deliver_audio(app, message, audio_file, metadata, task, download_url=download_url)
            await task.update(build_progress_message(DownloadPhase.COMPLETED), parse_mode=ParseMode.HTML)
        finally:
            await cleanup_paths([work_dir])

    async def _download_youtube_playlist(self, app: Client, message: Message, task: DownloadTask, info: dict[str, Any]) -> None:
        title = sanitize_filename(str(info.get("title") or f"youtube_playlist_{task.user_id}"))
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / f"{title}_{int(time.time())}")
        zip_path = settings.zip_dir / f"{playlist_dir.name}.zip"
        try:
            entries = info.get("entries") or []
            if not entries:
                raise RuntimeError("Playlist has no entries.")

            total = min(len(entries), settings.max_playlist_items)
            await task.update(
                build_playlist_status(DownloadPhase.CHECKING_CACHE, done=0, total=total),
                parse_mode=ParseMode.HTML,
            )

            # Check cache for each track and download cached ones from channel (batch query)
            cached_count = 0
            uncached_entries: list[dict[str, Any]] = []

            if settings.song_cache_channel_id:
                yt_cache_keys = [f"youtube:{e.get('id')}" for e in entries[:total] if e.get("id")]
                cached_map: dict[str, dict[str, Any]] = {}
                if yt_cache_keys:
                    cached_map = await song_cache.get_cached_songs_batch(yt_cache_keys)
                for i, entry in enumerate(entries[:total]):
                    if task.cancelled():
                        raise asyncio.CancelledError
                    yt_id = entry.get("id")
                    if yt_id:
                        cache_key = f"youtube:{yt_id}"
                        if cache_key in cached_map:
                            track = await self._retrieve_cached_track(app, cached_map[cache_key], playlist_dir)
                            if track:
                                cached_count += 1
                                continue
                    uncached_entries.append(entry)
            else:
                uncached_entries = list(entries[:total])

            # Download uncached tracks individually
            newly_downloaded: list[tuple[Path, dict[str, Any]]] = []
            failed_count = 0
            for j, entry in enumerate(uncached_entries):
                if task.cancelled():
                    raise asyncio.CancelledError
                yt_id = entry.get("id")
                if not yt_id:
                    continue
                yt_url = f"https://www.youtube.com/watch?v={yt_id}"
                done = cached_count + j + 1
                await task.update(
                    build_playlist_status(
                        DownloadPhase.DOWNLOADING,
                        done=done,
                        total=total,
                        cached=cached_count,
                        failed=failed_count,
                    ),
                    parse_mode=ParseMode.HTML,
                )
                work_dir = await ensure_clean_directory(settings.temp_dir / f"yt_{task.user_id}_{j}_{int(time.time())}")
                try:
                    duration = int(entry.get("duration") or 0)
                    audio_file, thumb_url, _ = await self._run_ytdlp_download(
                        task, yt_url, work_dir, timeout=max(duration * 3 + 300, 600),
                    )
                    entry["_thumb_url"] = thumb_url
                    dest = playlist_dir / sanitize_filename(f"{entry.get('title', 'Unknown')}.mp3")
                    shutil.move(str(audio_file), str(dest))
                    newly_downloaded.append((dest, entry))
                except Exception:
                    logger.exception("Failed to download track: %s", entry.get("title"))
                    failed_count += 1
                finally:
                    await cleanup_paths([work_dir])

            # Cache newly downloaded tracks
            if newly_downloaded:
                yt_entries_map: dict[int, dict[str, Any]] = {}
                for idx, entry in enumerate(uncached_entries, 1):
                    yt_entries_map[idx] = entry
                await self._cache_new_tracks(app, task, newly_downloaded, yt_entries_map)

            tracks = list_audio_files(playlist_dir)
            if not tracks:
                raise RuntimeError("Playlist download finished but no MP3 files were found.")

            await task.update(build_progress_message(DownloadPhase.PACKAGING, details=f"{len(tracks)} tracks"), parse_mode=ParseMode.HTML)
            await build_zip(playlist_dir, zip_path)
            zip_chat_id = settings.private_channel_id or settings.song_cache_channel_id
            upload = await upload_zip_to_storage(app, zip_path, caption=f"YouTube playlist archive for user {task.user_id}", chat_id=zip_chat_id)
            link = await link_store.create_ref(
                payload={
                    "user_id": task.user_id,
                    "chat_id": zip_chat_id,
                    "message_id": 0,
                    "file_id": upload.file_id,
                    "file_name": upload.file_name,
                    "file_size": upload.file_size,
                },
            )
            link = f"{settings.download_base_url.rstrip('/')}/generate/{link}"
            eta_seconds = estimate_download_time(upload.file_size, settings.download_speed_kbps)
            await task.update(
                build_playlist_completion(
                    track_count=len(tracks),
                    file_size=upload.file_size,
                    download_link=link,
                    estimated_time=eta_seconds,
                    speed_kbps=settings.download_speed_kbps,
                    cached_count=cached_count,
                    downloaded_count=len(newly_downloaded),
                    failed_count=failed_count,
                ),
                parse_mode=ParseMode.HTML,
            )
        finally:
            await cleanup_paths([playlist_dir, zip_path])

    async def _retrieve_cached_track(self, app: Client, cached: dict[str, Any], dest_dir: Path) -> Path | None:
        """Download a cached song from the cache channel to a local file."""
        try:
            file_id = cached["telegram_file_id"]
            title = cached.get("title", "Unknown")
            artist = cached.get("artist", "Unknown Artist")
            file_name = sanitize_filename(f"{artist} - {title}.mp3")
            dest_path = dest_dir / file_name
            # Avoid filename collisions when multiple tracks share generic metadata
            if dest_path.exists():
                cache_key = cached.get("cache_key", "")
                suffix = cache_key.split(":")[-1] if ":" in cache_key else ""
                if suffix:
                    stem = dest_path.stem
                    file_name = sanitize_filename(f"{stem} ({suffix}).mp3")
                    dest_path = dest_dir / file_name
                else:
                    n = 2
                    while dest_path.exists():
                        stem = dest_path.stem
                        file_name = sanitize_filename(f"{stem} ({n}).mp3")
                        dest_path = dest_dir / file_name
                        n += 1
            await app.download_media(file_id, file_name=str(dest_path))
            if dest_path.exists() and dest_path.stat().st_size > 0:
                return dest_path
        except Exception:
            logger.exception("Failed to retrieve cached track: %s - %s", cached.get("artist"), cached.get("title"))
        return None

    async def _get_spotify_playlist_track_urls(self, task: DownloadTask, out_dir: Path) -> list[str]:
        """Extract individual track URLs from a Spotify playlist using spotdl save."""
        import json

        save_file = out_dir / "_tracks.spotdl"
        cmd = [
            shutil.which("spotdl") or "spotdl",
            "save",
            task.request.source,
            "--save-file", str(save_file),
        ]
        if settings.spotify_client_id:
            cmd.extend(["--client-id", settings.spotify_client_id])
        if settings.spotify_client_secret:
            cmd.extend(["--client-secret", settings.spotify_client_secret])
        if settings.spotify_cookie_file:
            cookie_path = Path(settings.spotify_cookie_file)
            if cookie_path.is_file() and cookie_path.stat().st_size > 0:
                cmd.extend(["--cookie-file", settings.spotify_cookie_file])
        try:
            await self._run_subprocess(task, cmd, "spotdl")
        except SubprocessFailure:
            logger.warning("spotdl save failed, falling back to full playlist download")
            return []
        if save_file.exists():
            try:
                data = json.loads(save_file.read_text())
                urls = [song["url"] for song in data if isinstance(song, dict) and "url" in song]
                return urls
            except (json.JSONDecodeError, KeyError):
                logger.warning("Failed to parse spotdl save output")
            finally:
                save_file.unlink(missing_ok=True)
        return []

    async def _cache_new_tracks(
        self,
        app: Client,
        task: DownloadTask,
        tracks: list[tuple[Path, dict[str, Any]]],
        yt_entries: dict[int, dict[str, Any]] | None = None,
    ) -> int:
        """Cache newly downloaded playlist tracks. Returns count cached."""
        if not settings.song_cache_channel_id or not tracks:
            return 0

        cached_count = 0
        total = len(tracks)

        for i, (track_path, entry) in enumerate(tracks, 1):
            if task.cancelled():
                return cached_count
            try:
                metadata = await read_audio_metadata(
                    track_path,
                    fallback_title=str(entry.get("title") or track_path.stem),
                    fallback_artist=str(entry.get("uploader") or entry.get("channel") or "Unknown Artist"),
                )
                cache_key: str | None = None

                yt_id = entry.get("id")
                if yt_id:
                    cache_key = f"youtube:{yt_id}"

                if not cache_key or await song_cache.get_cached_song(cache_key):
                    continue

                thumb_url = entry.get("_thumb_url") or (yt_entries.get(i, {}).get("thumbnail") if yt_entries else None)
                thumb_path: Path | None = None
                if thumb_url:
                    try:
                        thumb_path = await extract_thumbnail_from_url(
                            thumb_url, track_path.parent / f"_cthumb_{i}.jpg",
                        )
                    except Exception:
                        pass

                audio_file_id, thumb_file_id = await song_cache.upload_to_cache_channel(
                    app, track_path, metadata.title, metadata.artist, metadata.duration, thumb_path,
                )
                file_size = track_path.stat().st_size
                ref = await link_store.create_ref(
                    payload={
                        "user_id": task.user_id,
                        "chat_id": settings.song_cache_channel_id,
                        "message_id": 0,
                        "file_id": audio_file_id,
                        "file_name": f"{metadata.artist} - {metadata.title}.mp3",
                        "file_size": file_size,
                    },
                )
                download_url = f"{settings.download_base_url.rstrip('/')}/generate/{ref}"
                await song_cache.cache_song(
                    cache_key=cache_key,
                    key_type="youtube",
                    file_id=audio_file_id,
                    title=metadata.title,
                    artist=metadata.artist,
                    duration=metadata.duration,
                    file_size=file_size,
                    thumbnail_file_id=thumb_file_id,
                    download_link=download_url,
                )

                if thumb_path:
                    thumb_path.unlink(missing_ok=True)

                cached_count += 1
            except Exception:
                logger.exception("Failed to cache track: %s", track_path.name)

        if cached_count:
            logger.info("Cached %d/%d new tracks", cached_count, total)
        return cached_count

    async def _cache_spotify_tracks(
        self,
        app: Client,
        task: DownloadTask,
        tracks: list[tuple[Path, str]],
    ) -> int:
        """Cache newly downloaded Spotify playlist tracks using their Spotify track IDs."""
        if not settings.song_cache_channel_id or not tracks:
            return 0

        cached_count = 0
        total = len(tracks)

        for i, (track_path, spotify_url) in enumerate(tracks, 1):
            if task.cancelled():
                return cached_count
            try:
                metadata = await read_audio_metadata(
                    track_path,
                    fallback_title=track_path.stem,
                    fallback_artist="Unknown Artist",
                )

                # Use Spotify track ID as cache key
                cache_key, _ = generate_cache_key(spotify_url, InputType.SPOTIFY_TRACK)
                if not cache_key or await song_cache.get_cached_song(cache_key):
                    continue

                audio_file_id, thumb_file_id = await song_cache.upload_to_cache_channel(
                    app, track_path, metadata.title, metadata.artist, metadata.duration,
                )
                file_size = track_path.stat().st_size
                ref = await link_store.create_ref(
                    payload={
                        "user_id": task.user_id,
                        "chat_id": settings.song_cache_channel_id,
                        "message_id": 0,
                        "file_id": audio_file_id,
                        "file_name": f"{metadata.artist} - {metadata.title}.mp3",
                        "file_size": file_size,
                    },
                )
                download_url = f"{settings.download_base_url.rstrip('/')}/generate/{ref}"
                await song_cache.cache_song(
                    cache_key=cache_key,
                    key_type="spotify",
                    file_id=audio_file_id,
                    title=metadata.title,
                    artist=metadata.artist,
                    duration=metadata.duration,
                    file_size=file_size,
                    thumbnail_file_id=thumb_file_id,
                    download_link=download_url,
                )
                cached_count += 1
            except Exception:
                logger.exception("Failed to cache Spotify track: %s", spotify_url)

        if cached_count:
            logger.info("Cached %d/%d Spotify tracks", cached_count, total)
        return cached_count

    async def _resolve_search_and_check_cache(self, app: Client, message: Message, task: DownloadTask) -> str | None:
        """Resolve a search query to a YouTube URL, checking cache.

        Returns the YouTube URL if not cached (caller should download).
        Returns None if cache hit (song already sent to user).
        Returns _RESOLVE_FAILED if resolution failed entirely.
        """
        try:
            search_info = await extract_info(f"ytsearch1:{task.request.source}")
            if search_info is None:
                return _RESOLVE_FAILED  # type: ignore[return-value]
            entries = search_info.get("entries") or []
            if not entries:
                return _RESOLVE_FAILED  # type: ignore[return-value]
            entry = entries[0]
            yt_id = entry.get("id")
            if not yt_id:
                return _RESOLVE_FAILED  # type: ignore[return-value]

            cache_key, _ = generate_cache_key(task.request.source, task.request.input_type, entry)
            if cache_key:
                cached = await song_cache.get_cached_song(cache_key)
                if cached:
                    try:
                        await self._send_cached_audio(app, message, cached, task)
                        await task.update(build_progress_message(DownloadPhase.COMPLETED), parse_mode=ParseMode.HTML)
                        return None  # Cache hit
                    except Exception:
                        logger.warning("Cached file send failed for %s, re-downloading", cache_key)
                        await song_cache.invalidate_cache(cache_key)

            return f"https://www.youtube.com/watch?v={yt_id}"
        except Exception:
            logger.exception("Search resolution failed, falling back to ytsearch download")
            return _RESOLVE_FAILED  # type: ignore[return-value]

    async def _send_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any, task: DownloadTask, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        caption = build_audio_caption(
            title=metadata.title,
            artist=metadata.artist,
            duration=metadata.duration,
        )
        await app.send_audio(
            chat_id=message.chat.id,
            audio=str(audio_file),
            caption=caption,
            caption_entities=None,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=task.original_message_id,
            reply_markup=reply_markup,
            title=metadata.title,
            performer=metadata.artist,
            duration=metadata.duration,
            thumb=str(metadata.thumbnail_path) if metadata.thumbnail_path and metadata.thumbnail_path.exists() else None,
        )

    async def _get_bot_username(self, app: Client) -> str | None:
        global _cached_bot_username
        if _cached_bot_username:
            return _cached_bot_username
        try:
            me = await app.get_me()
            if me and me.username:
                _cached_bot_username = me.username
                return _cached_bot_username
        except Exception:
            pass
        return None

    async def _send_cached_audio(self, app: Client, message: Message, cached: dict[str, Any], task: DownloadTask) -> None:
        """Send a cached song to the user using the stored Telegram file_id."""
        username = await self._get_bot_username(app)
        ref = await link_store.create_ref(
            payload={
                "user_id": message.from_user.id,
                "chat_id": settings.song_cache_channel_id,
                "message_id": 0,
                "file_id": cached["telegram_file_id"],
                "file_name": f"{cached['artist']} - {cached['title']}.mp3",
                "file_size": cached["file_size"],
            },
        )
        download_url = f"{settings.download_base_url.rstrip('/')}/generate/{ref}"
        audio_markup = build_audio_keyboard(username, download_url=download_url) if username else None
        caption = build_audio_caption(
            title=cached["title"],
            artist=cached["artist"],
            duration=cached["duration"],
        )
        thumb = cached.get("thumbnail_file_id") or None
        await app.send_audio(
            chat_id=message.chat.id,
            audio=cached["telegram_file_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=task.original_message_id,
            reply_markup=audio_markup,
            title=cached["title"],
            performer=cached["artist"],
            duration=cached["duration"],
            thumb=thumb,
        )

    async def _send_large_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any, task: DownloadTask) -> None:
        """Upload large audio (>50MB) to private channel and send download link."""
        upload = await upload_zip_to_storage(app, audio_file, caption=f"{metadata.title} - {metadata.artist}")
        link = await link_store.create_link(
            user_id=task.user_id,
            payload={
                "chat_id": settings.private_channel_id,
                "message_id": upload.message_id,
                "file_id": upload.file_id,
                "file_name": upload.file_name,
                "file_size": upload.file_size,
            },
        )
        eta_seconds = estimate_download_time(upload.file_size, settings.download_speed_kbps)
        text = build_large_file_message(
            title=metadata.title,
            artist=metadata.artist,
            duration=metadata.duration,
            file_size=upload.file_size,
            download_link=link,
            estimated_time=eta_seconds,
            speed_kbps=settings.download_speed_kbps,
        )
        await app.send_message(
            chat_id=message.chat.id,
            text=text,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML,
        )

    async def _deliver_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any, task: DownloadTask, download_url: str | None = None) -> None:
        """Send audio directly if under 2GB, otherwise upload to channel and send link."""
        file_size = audio_file.stat().st_size
        if file_size <= _TELEGRAM_BOT_UPLOAD_LIMIT:
            username = await self._get_bot_username(app)
            audio_markup = build_audio_keyboard(username, download_url=download_url) if username else None
            await self._send_audio(app, message, audio_file, metadata, task, reply_markup=audio_markup)
        else:
            await self._send_large_audio(app, message, audio_file, metadata, task)

    async def _run_spotdl(
        self,
        task: DownloadTask,
        source: str,
        out_dir: Path,
        playlist: bool,
        audio_providers: tuple[str, ...] = ("youtube-music", "youtube"),
    ) -> SubprocessResult:
        cmd = [
            shutil.which("spotdl") or "spotdl",
            "download",
            source,
            "--headless",
            "--output",
            str(out_dir / "{artists} - {title}.{output-ext}"),
            "--overwrite",
            "skip",
            "--threads",
            "1",
            "--audio",
            *audio_providers,
            "--bitrate",
            "320k",
            "--format",
            "mp3",
        ]
        if settings.spotify_client_id:
            cmd.extend(["--client-id", settings.spotify_client_id])
        if settings.spotify_client_secret:
            cmd.extend(["--client-secret", settings.spotify_client_secret])
        if settings.spotify_cookie_file:
            cookie_path = Path(settings.spotify_cookie_file)
            if cookie_path.is_file() and cookie_path.stat().st_size > 0:
                cmd.extend(["--cookie-file", settings.spotify_cookie_file])
        return await self._run_subprocess(task, cmd, "spotdl")

    async def _run_spotdl_batch(
        self,
        task: DownloadTask,
        urls: list[str],
        out_dir: Path,
        total_tracks: int = 0,
        done_offset: int = 0,
        audio_providers: tuple[str, ...] = ("youtube-music", "youtube"),
    ) -> SubprocessResult:
        """Run spotdl download with multiple URLs in a single process to avoid per-track rate limiting."""
        cmd = [
            shutil.which("spotdl") or "spotdl",
            "download",
            *urls,
            "--headless",
            "--output",
            str(out_dir / "{artists} - {title}.{output-ext}"),
            "--overwrite",
            "skip",
            "--threads",
            "1",
            "--audio",
            *audio_providers,
            "--bitrate",
            "320k",
            "--format",
            "mp3",
        ]
        if settings.spotify_client_id:
            cmd.extend(["--client-id", settings.spotify_client_id])
        if settings.spotify_client_secret:
            cmd.extend(["--client-secret", settings.spotify_client_secret])
        if settings.spotify_cookie_file:
            cookie_path = Path(settings.spotify_cookie_file)
            if cookie_path.is_file() and cookie_path.stat().st_size > 0:
                cmd.extend(["--cookie-file", settings.spotify_cookie_file])
        return await self._run_subprocess(task, cmd, "spotdl", total_tracks=total_tracks, done_offset=done_offset)

    _FIRST_OUTPUT_TIMEOUT: int = 60
    _STALL_TIMEOUT: int = 90

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        """Kill the entire process group to ensure child processes are also terminated."""
        import os
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.kill()
        except ProcessLookupError:
            pass

    async def _run_subprocess(
        self,
        task: DownloadTask,
        cmd: list[str],
        name: str,
        *,
        total_tracks: int = 0,
        done_offset: int = 0,
    ) -> SubprocessResult:
        logger.info("Running %s command: %s", name, shlex.join(cmd))
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        recent_lines: deque[str] = deque(maxlen=50)
        error_lines: deque[str] = deque(maxlen=20)
        has_output = False
        spotdl_state: dict[str, int] = (
            {"total": total_tracks, "done": done_offset} if name == "spotdl" else {}
        )
        try:
            while True:
                if task.cancelled():
                    self._kill_process_group(process)
                    raise asyncio.CancelledError
                timeout = settings.spotdl_inactivity_timeout_seconds if has_output else self._FIRST_OUTPUT_TIMEOUT
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError as exc:
                    self._kill_process_group(process)
                    last_detail = recent_lines[-1] if recent_lines else "No output was produced."
                    raise SubprocessFailure(
                        f"{name} stalled after {int(timeout)} seconds. "
                        f"Last output: {last_detail}",
                        SubprocessResult(recent_lines=tuple(recent_lines), error_lines=tuple(error_lines)),
                    ) from exc
                has_output = True
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    recent_lines.append(text)
                    is_error = self._is_subprocess_error_line(name, text)
                    if is_error:
                        error_lines.append(text)
                    logger.info("%s: %s", name, text)
                    if not is_error:
                        progress_text = self._map_subprocess_progress(name, text, spotdl_state)
                        if progress_text:
                            await task.update(progress_text[:4000], parse_mode=ParseMode.HTML)
            code = await process.wait()
            if code != 0:
                last_detail = error_lines[-1] if error_lines else (recent_lines[-1] if recent_lines else "No error details captured.")
                raise SubprocessFailure(
                    f"{name} exited with code {code}. Last output: {last_detail}",
                    SubprocessResult(recent_lines=tuple(recent_lines), error_lines=tuple(error_lines)),
                )
            if error_lines:
                logger.warning("%s exited successfully but produced error lines: %s", name, error_lines[-1])
            return SubprocessResult(recent_lines=tuple(recent_lines), error_lines=tuple(error_lines))
        finally:
            self._kill_process_group(process)
            await process.wait()

    def _map_subprocess_progress(self, name: str, text: str, spotdl_state: dict[str, int] | None = None) -> str | None:
        lowered = text.lower()
        if name != "spotdl":
            if "download" in lowered or "converting" in lowered or "processing" in lowered:
                return build_progress_message(DownloadPhase.DOWNLOADING)
            return None

        if "processing query" in lowered:
            return build_progress_message(DownloadPhase.SEARCHING)

        # Track total song count from "Found X songs" line
        if spotdl_state is not None:
            total_match = re.search(r"found\s+(\d+)\s+song", lowered)
            if total_match:
                total = int(total_match.group(1))
                spotdl_state["total"] = total
                return build_playlist_status(DownloadPhase.SEARCHING, done=0, total=total)

        if "saved" in lowered and "song" in lowered:
            saved_match = re.search(r"saved\s+(\d+)\s+song", lowered)
            if saved_match and spotdl_state is not None:
                spotdl_state["done"] += int(saved_match.group(1))
                return build_playlist_status(DownloadPhase.SEARCHING, done=spotdl_state["done"], total=spotdl_state["total"])

        if "rate" in lowered and "limit" in lowered:
            return None  # Don't spam status with rate limit messages

        if "download" in lowered:
            # Extract song name from spotdl "Downloaded" lines
            match = re.search(r'Downloaded\s+"(.+?)"', text)
            track = match.group(1) if match else None
            if spotdl_state is not None:
                spotdl_state["done"] += 1
                done = spotdl_state["done"]
                total = spotdl_state["total"]
                if total > 0:
                    return build_playlist_status(DownloadPhase.DOWNLOADING, done=done, total=total)
                if track:
                    return build_progress_message(DownloadPhase.DOWNLOADING, details=f"♫ {track}")
            elif track:
                return build_progress_message(DownloadPhase.DOWNLOADING, details=f"♫ {track}")
            return build_progress_message(DownloadPhase.DOWNLOADING)
        if "converting" in lowered:
            return build_progress_message(DownloadPhase.CONVERTING)
        if "skipping" in lowered:
            return f"<i>⏭ {escape_html(text)}</i>"
        return None

    def _is_subprocess_error_line(self, name: str, text: str) -> bool:
        lowered = text.lower()
        if name == "spotdl":
            return any(
                marker in lowered
                for marker in (
                    "error:",
                    "audioprovidererror",
                    "ffmpegerror",
                    "lookuperror",
                    "download error",
                    "failed",
                )
            )
        return "error" in lowered or "failed" in lowered

    def _extract_youtube_url(self, result: SubprocessResult) -> str | None:
        all_lines = (*result.error_lines, *result.recent_lines)
        for text in reversed(all_lines):
            match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)\S+", text)
            if match:
                url = match.group(0).rstrip(".,;:!)\"]'")
                return url
        return None

    @staticmethod
    def _extract_youtube_urls_batch(result: SubprocessResult) -> list[str]:
        """Extract all YouTube URLs from spotdl batch output in processing order."""
        urls: list[str] = []
        all_lines = (*result.error_lines, *result.recent_lines)
        for text in all_lines:
            match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)\S+", text)
            if match:
                url = match.group(0).rstrip(".,;:!)\"]'")
                urls.append(url)
        return urls


    async def _validate_audio_file(self, file_path: Path) -> float:
        """Check that a downloaded file is a valid audio file using ffprobe.

        Returns the audio duration in seconds.
        """
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type,duration",
            "-of", "csv=p=0",
            str(file_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"ffprobe timed out — downloaded file may be corrupted")
        if proc.returncode != 0 or b"audio" not in stdout:
            size_kb = file_path.stat().st_size / 1024 if file_path.exists() else 0
            raise RuntimeError(f"Downloaded file is not valid audio ({size_kb:.0f} KB)")
        # Parse duration from the last field of the ffprobe CSV output
        parts = stdout.strip().split(b",")
        try:
            return float(parts[-1]) if len(parts) > 1 else 0.0
        except (ValueError, IndexError):
            return 0.0

    async def _embed_cover_in_mp3(self, mp3_path: Path, cover_url: str | None = None) -> None:
        """Embed cover art into an existing MP3 file using FFmpeg."""
        if not cover_url or not mp3_path.exists():
            return
        try:
            # Use unique temp names to avoid conflicts in concurrent batch embedding
            cover = await extract_thumbnail_from_url(cover_url, mp3_path.parent / f"_cover_{mp3_path.stem}.jpg")
            if not cover:
                return
            temp_path = mp3_path.parent / f"{mp3_path.stem}._cover_embed.tmp.mp3"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", str(mp3_path),
                "-i", str(cover),
                "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-metadata:s:v", "comment=Cover (front)",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(temp_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            cover.unlink(missing_ok=True)
            if proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                temp_path.replace(mp3_path)
            else:
                temp_path.unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to embed cover art in %s", mp3_path.name)

    async def _embed_cover_for_file(self, mp3_path: Path, yt_url: str | None) -> None:
        """Extract YouTube video ID from URL and embed cover art."""
        if not yt_url or not mp3_path.exists():
            return
        yt_id_match = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", yt_url)
        if yt_id_match:
            thumb_url = f"https://i.ytimg.com/vi/{yt_id_match.group(1)}/maxresdefault.jpg"
            await self._embed_cover_in_mp3(mp3_path, thumb_url)

    _MAX_CONCURRENT_EMBEDS = 3

    async def _embed_cover_art_batch(self, files: list[Path], yt_urls: list[str]) -> None:
        """Embed cover art in multiple MP3 files concurrently (limited concurrency)."""
        if not files or not yt_urls:
            return

        sem = asyncio.Semaphore(self._MAX_CONCURRENT_EMBEDS)

        async def _embed(idx: int, audio_file: Path) -> None:
            async with sem:
                if idx < len(yt_urls):
                    yt_id_match = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", yt_urls[idx])
                    if yt_id_match:
                        thumb_url = f"https://i.ytimg.com/vi/{yt_id_match.group(1)}/maxresdefault.jpg"
                        await self._embed_cover_in_mp3(audio_file, thumb_url)

        await asyncio.gather(*[_embed(i, f) for i, f in enumerate(files)])

    async def _convert_to_mp3(self, input_path: Path, task: DownloadTask, timeout: float | None = None, *, title: str | None = None, artist: str | None = None, cover_path: Path | None = None) -> Path:
        """Convert an audio file to MP3 using FFmpeg as a subprocess with a timeout."""
        import os
        import signal

        duration = await self._validate_audio_file(input_path)
        # Dynamic timeout: scale with audio duration (6x real-time + 300s overhead, minimum 900s)
        if timeout is None:
            timeout = max(int(duration) * 6 + 300, _CONVERSION_TIMEOUT_BASE)
        output_path = input_path.with_suffix(".mp3")
        text = build_progress_message(DownloadPhase.CONVERTING, details="Converting to MP3...")
        await task.update(text, parse_mode=ParseMode.HTML)

        cmd = [
            "ffmpeg", "-y",
            "-analyzeduration", "10M", "-probesize", "10M",
            "-i", str(input_path),
        ]
        if cover_path and cover_path.exists():
            cmd.extend(["-i", str(cover_path), "-map", "0:a", "-map", "1:v", "-c:v", "copy"])
            cmd.extend(["-metadata:s:v", "comment=Cover (front)"])
        else:
            cmd.extend(["-map_metadata", "0", "-vn"])
        cmd.extend(["-codec:a", "libmp3lame", "-b:a", "320k"])
        if title:
            cmd.extend(["-metadata", f"title={title}"])
        if artist:
            cmd.extend(["-metadata", f"artist={artist}"])
        cmd.append(str(output_path))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            await proc.wait()
            raise RuntimeError(f"FFmpeg conversion timed out after {timeout}s")
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"FFmpeg conversion failed (exit {proc.returncode}): {err}")
        input_path.unlink(missing_ok=True)
        return output_path

    async def _run_ytdlp_download(self, task: DownloadTask, url: str, out_dir: Path, timeout: float = 600) -> tuple[Path, str | None, dict[str, Any] | None]:
        loop = asyncio.get_running_loop()
        last_progress_time = [0.0]  # mutable container for throttle
        last_hook_time = [time.monotonic()]  # for stall detection

        def progress_hook(payload: dict[str, Any]) -> None:
            now = time.monotonic()
            last_hook_time[0] = now
            if now - last_progress_time[0] < _PROGRESS_UPDATE_INTERVAL:
                return
            status = payload.get("status")
            if status == "downloading":
                total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
                downloaded = payload.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0
                eta = payload.get("eta")  # seconds remaining from yt-dlp
                speed = payload.get("speed")  # bytes/sec from yt-dlp
                speed_kbps = (speed / 1024) if speed else None
                text = build_progress_message(DownloadPhase.DOWNLOADING, percentage=percent, eta=eta, speed_kbps=speed_kbps)
                last_progress_time[0] = now
                asyncio.run_coroutine_threadsafe(task.update(text, parse_mode=ParseMode.HTML), loop)

        output_template = str(out_dir / "%(title)s.%(ext)s")
        ydl_opts = {
            **_base_ytdlp_opts(),
            "format": "ba[ext=m4a]/ba[acodec!=none]/ba/b",
            "outtmpl": output_template,
            "noplaylist": True,
            "windowsfilenames": True,
            "restrictfilenames": True,
            "progress_hooks": [progress_hook],
        }

        def _download() -> tuple[Path, str | None, dict[str, Any] | None]:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            thumb_url = None
            entry: dict[str, Any] | None = None
            if info:
                if "entries" in info:
                    entry = info["entries"][0] if info["entries"] else None
                else:
                    entry = info
            if entry:
                thumb_url = entry.get("thumbnail")
            # Find the downloaded audio file (webm, m4a, opus, etc.)
            audio_exts = (".webm", ".m4a", ".opus", ".mp3", ".wav", ".flac", ".aac", ".ogg")
            file_path = None
            for f in sorted(out_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in audio_exts:
                    file_path = f
                    break
            if not file_path:
                raise RuntimeError("Download failed: no output file.")
            if file_path.stat().st_size == 0:
                raise RuntimeError("Downloaded file is empty (0 bytes).")
            return file_path, thumb_url, entry

        download_task = asyncio.create_task(asyncio.to_thread(_download))
        try:
            while True:
                try:
                    raw_path, thumb_url, entry = await asyncio.wait_for(
                        asyncio.shield(download_task), timeout=10,
                    )
                    break
                except asyncio.TimeoutError:
                    if download_task.done():
                        raw_path, thumb_url, entry = download_task.result()
                        break
                    if time.monotonic() - last_hook_time[0] > self._STALL_TIMEOUT:
                        raise RuntimeError(
                            f"Download stalled: no progress for {self._STALL_TIMEOUT}s"
                        )
        finally:
            if not download_task.done():
                download_task.cancel()

        # Convert to MP3 outside of yt-dlp so we can control the timeout
        if raw_path.suffix.lower() != ".mp3":
            _title = str(entry.get("title") or "") if entry else None
            _artist = str(entry.get("uploader") or entry.get("channel") or "") if entry else None
            _cover: Path | None = None
            if thumb_url:
                try:
                    _cover = await extract_thumbnail_from_url(thumb_url, raw_path.parent / f"_cover_{raw_path.stem}.jpg")
                except Exception:
                    pass
            raw_path = await self._convert_to_mp3(raw_path, task, title=_title, artist=_artist, cover_path=_cover)
            # Clean up temporary cover file
            if _cover:
                _cover.unlink(missing_ok=True)

        return raw_path, thumb_url, entry


download_manager = MusicDownloadManager()
