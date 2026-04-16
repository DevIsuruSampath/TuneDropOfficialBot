from __future__ import annotations

import asyncio
from collections import deque
import json

try:
    import orjson as _json
    def _json_loads(s: str) -> Any:
        return _json.loads(s)
except ImportError:
    def _json_loads(s: str) -> Any:
        return json.loads(s)
import logging
import os
import re
import shlex
import shutil
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardMarkup, Message
from yt_dlp import YoutubeDL

from tunedrop.app.core.config import settings
from tunedrop.app.services.cache_service import generate_cache_key, song_cache
from tunedrop.app.services.link_generator import link_store
from tunedrop.app.services.metadata import read_audio_metadata
from tunedrop.app.services.progress import DownloadTask
from tunedrop.app.services.uploader import copy_audio_to_stream, upload_zip_to_storage
from tunedrop.app.core.client import get_client_by_index, get_client_index
from tunedrop.app.services.youtube_service import _base_ytdlp_opts, extract_info, search_youtube_music
from tunedrop.app.services.zip_service import build_zip
from tunedrop.app.utils.ffmpeg_utils import extract_cover_from_mp3, extract_thumbnail_from_url, extract_youtube_thumbnail
from tunedrop.app.utils.file_utils import (
    cleanup_paths,
    ensure_clean_directory,
    find_first_file,
    list_audio_files,
    sanitize_filename,
)
from tunedrop.app.utils.search_utils import build_search_candidates, extract_youtube_id, first_valid_entry
from tunedrop.app.utils.time_utils import estimate_download_time
from tunedrop.app.services.subscription import subscription_service
from tunedrop.app.utils.ui_utils import (
    DownloadPhase,
    build_audio_caption,
    build_audio_keyboard,
    build_free_delivery_message,
    build_large_file_message,
    build_playlist_completion,
    build_playlist_status,
    build_progress_message,
    escape_html,
)
from tunedrop.app.utils.validators import InputType
from tunedrop.app.utils.perf import timed


logger = logging.getLogger(__name__)

_SPOTDL_BIN = shutil.which("spotdl") or "spotdl"  # resolved once at import time
_TELEGRAM_BOT_UPLOAD_LIMIT = 4 * 1024 * 1024 * 1024  # 4GB (MTProto/Premium bot limit)
_PROGRESS_UPDATE_INTERVAL = 2.0  # seconds between Telegram message edits
_CONVERSION_TIMEOUT_BASE = 900  # minimum seconds for FFmpeg audio conversion
_MAX_AUDIO_DURATION = 3 * 3600  # 3 hours
_RESOLVE_FAILED = object()  # Sentinel: search resolution failed, not a cache hit
_UPLOAD_SEMAPHORE = 4       # Parallel send_audio (cache uploads) — real limit is per-client in uploader.py
_SPOTIFY_PLAYLIST_DOWNLOAD_CONCURRENCY = 4
_YOUTUBE_PLAYLIST_DOWNLOAD_CONCURRENCY = 3
_bot_usernames: dict[int, str] = {}  # Per-client bot username cache (keyed by id(app))


def _build_display_name(artist: str, title: str) -> str:
    """Build 'Artist - Title' string, skipping artist prefix if title already contains it."""
    cleaned_artist = re.sub(r"\s+", "", artist).lower()
    cleaned_title_start = re.sub(r"\s+", "", title[: len(artist) + 3]).lower()
    if cleaned_title_start.startswith(cleaned_artist):
        return title
    return f"{artist} - {title}"


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
    _spotify_track_search_queries: dict[str, str] = {}

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

    @timed("download_spotify_track")
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
                    logger.warning("Cached file send failed for %s, re-downloading", cache_key, exc_info=True)
                    await song_cache.invalidate_cache(cache_key)

        work_dir = await ensure_clean_directory(settings.temp_dir / f"sp_{task.task_id}")
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
            attempted_search_candidates: set[str] = set()

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
                    if task.request.input_type == InputType.SEARCH:
                        audio_file, thumb_url, yt_info, attempted_search_candidates = await self._run_search_query_downloads(
                            task,
                            work_dir,
                            attempted_search_candidates,
                        )
                    else:
                        yt_url = f"ytsearch1:{task.request.source}"

                if yt_url and task.request.input_type != InputType.SEARCH:
                    await task.update(build_progress_message(DownloadPhase.SEARCHING, details="Trying alternative source..."), parse_mode=ParseMode.HTML)
                elif yt_url and task.request.input_type == InputType.SEARCH:
                    await task.update(build_progress_message(DownloadPhase.SEARCHING), parse_mode=ParseMode.HTML)

                if yt_url:
                    try:
                        audio_file, thumb_url, yt_info = await self._run_ytdlp_download(task, yt_url, work_dir)
                    except Exception:
                        logger.exception("yt-dlp download failed")

                # Fallback: if resolved URL failed, try raw ytsearch variants
                if (
                    not audio_file
                    and task.request.input_type == InputType.SEARCH
                    and yt_url
                    and not yt_url.startswith("ytsearch")
                ):
                    audio_file, thumb_url, yt_info, attempted_search_candidates = await self._run_search_query_downloads(
                        task,
                        work_dir,
                        attempted_search_candidates,
                    )
                elif not audio_file and yt_url and not yt_url.startswith("ytsearch"):
                    yt_url = f"ytsearch1:{task.request.source}"
                    logger.info("Retrying with ytsearch fallback: %s", yt_url)
                    await task.update(build_progress_message(DownloadPhase.SEARCHING, details="Trying alternative..."), parse_mode=ParseMode.HTML)
                    try:
                        audio_file, thumb_url, yt_info = await self._run_ytdlp_download(task, yt_url, work_dir)
                    except Exception:
                        logger.exception("ytsearch fallback also failed")

                audio_file = find_first_file(work_dir, suffix=".mp3")

            if not audio_file:
                raise RuntimeError("Could not download the track. Please try again later.")

            fallback_title = audio_file.stem
            fallback_artist = "Unknown Artist"
            if yt_info:
                fallback_title = str(yt_info.get("title") or fallback_title)
                fallback_artist = str(yt_info.get("uploader") or yt_info.get("channel") or fallback_artist)
            metadata = await read_audio_metadata(audio_file, fallback_title=fallback_title, fallback_artist=fallback_artist)
            thumb_path: Path | None = None
            if thumb_url:
                # Try multiple YouTube thumbnail sizes
                yt_id_match = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", thumb_url)
                if yt_id_match:
                    thumb_path = await extract_youtube_thumbnail(yt_id_match.group(1), work_dir / "thumb.jpg")
                if not thumb_path:
                    thumb_path = await extract_thumbnail_from_url(thumb_url, work_dir / "thumb.jpg")
            if not thumb_path and audio_file:
                # Fallback: extract embedded cover art from the MP3 file
                thumb_path = await extract_cover_from_mp3(audio_file, work_dir / "thumb.jpg")
            metadata.thumbnail_path = thumb_path
            file_size = audio_file.stat().st_size

            # Cache the downloaded song
            download_url: str | None = None
            cache_file_id: str | None = None
            cache_bot_index: int | None = None
            cache_key, cache_key_type = generate_cache_key(task.request.source, task.request.input_type, yt_info)
            if cache_key:
                try:
                    await task.update(build_progress_message(DownloadPhase.UPLOADING), parse_mode=ParseMode.HTML)
                    cache_msg_id, audio_file_id, thumb_file_id, upload_bot_index = await song_cache.upload_to_cache_channel(
                        app, audio_file, metadata.title, metadata.artist, metadata.duration, metadata.thumbnail_path,
                        cache_key=cache_key,
                    )
                    stream_chat_id = settings.song_cache_channel_id
                    stream_msg_id = cache_msg_id
                    stream_bot_index = upload_bot_index
                    flog_file_id: str | None = None
                    flog_msg_id: int | None = None
                    upload_bot = get_client_by_index(upload_bot_index) or app
                    stream_copy = await copy_audio_to_stream(
                        upload_bot, audio_file_id, metadata.title, metadata.artist, metadata.duration, file_size, thumb_file_id,
                    )
                    if stream_copy:
                        stream_file_id = stream_copy.file_id
                        stream_chat_id = settings.stream_channel_id
                        stream_msg_id = stream_copy.message_id
                        stream_bot_index = stream_copy.bot_index
                        flog_file_id = stream_copy.file_id
                        flog_msg_id = stream_copy.message_id
                    file_name = f"{_build_display_name(metadata.artist, metadata.title)}.mp3"
                    ref = await link_store.create_ref(
                        payload={
                            "user_id": task.user_id,
                            "chat_id": stream_chat_id,
                            "message_id": stream_msg_id,
                            "file_id": stream_file_id,
                            "file_name": file_name,
                            "file_size": file_size,
                            "bot_index": stream_bot_index,
                        },
                    )
                    download_url = f"{settings.download_base_url.rstrip('/')}/d/{ref}"
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
                        cache_message_id=cache_msg_id,
                        flog_file_id=flog_file_id,
                        flog_message_id=flog_msg_id,
                        bot_index=upload_bot_index,
                    )
                except Exception:
                    logger.exception("Failed to cache song, sending directly to user")
                else:
                    cache_file_id = audio_file_id
                    cache_bot_index = upload_bot_index

            await self._deliver_audio(app, message, audio_file, metadata, task, download_url=download_url, cache_file_id=cache_file_id, cache_bot_index=cache_bot_index)
            await task.update(build_progress_message(DownloadPhase.COMPLETED), parse_mode=ParseMode.HTML)
        finally:
            await cleanup_paths([work_dir])

    async def _download_spotify_playlist(self, app: Client, message: Message, task: DownloadTask) -> None:
        safe_name = f"spotify_playlist_{task.task_id}"
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / safe_name)
        zip_path = settings.zip_dir / f"{safe_name}.zip"
        try:
            await task.update("🔍 <b>Reading playlist...</b>", parse_mode=ParseMode.HTML)


            # Get individual track URLs from the playlist using spotdl
            track_urls = await self._get_spotify_playlist_track_urls(task, playlist_dir)
            if not track_urls:
                raise RuntimeError("Could not retrieve playlist tracks.")

            total = len(track_urls)
            logger.info("Playlist has %d tracks, starting cache check", total)
            await task.update(
                build_playlist_status(DownloadPhase.CHECKING_CACHE, done=0, total=total),
                parse_mode=ParseMode.HTML,
            )

            # Separate cached and uncached tracks (batch DB query — no file downloads)
            uncached_urls: list[str] = []
            cached_urls: list[str] = []  # URLs that were in cache at check time
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
                            cached_count += 1
                            cached_urls.append(url)
                        else:
                            uncached_urls.append(url)
                        if i % 5 == 0 or i == total:
                            await task.update(
                                build_playlist_status(
                                    DownloadPhase.CHECKING_CACHE,
                                    done=i,
                                    total=total,
                                    cached=cached_count,
                                ),
                                parse_mode=ParseMode.HTML,
                            )
                else:
                    uncached_urls = track_urls
            else:
                uncached_urls = track_urls

            # Download uncached tracks in parallel (individual spotdl processes)
            newly_downloaded: list[tuple[Path, str]] = []  # (path, spotify_url)
            failed_count = 0
            if uncached_urls:
                _dl_sem = asyncio.Semaphore(_SPOTIFY_PLAYLIST_DOWNLOAD_CONCURRENCY)
                _dl_lock = asyncio.Lock()
                _dl_done_count = 0  # shared counter for progress

                async def _download_one(idx: int, url: str) -> None:
                    nonlocal failed_count, _dl_done_count
                    if task.cancelled():
                        return
                    async with _dl_sem:
                        if task.cancelled():
                            return
                        work_dir = await ensure_clean_directory(
                            settings.temp_dir / f"sp_{task.task_id}_{idx}"
                        )
                        yt_url = None
                        try:
                            # Phase 1: Try spotdl (resolves Spotify → YouTube Music)
                            try:
                                result = await self._run_spotdl(task, url, work_dir, playlist=False)
                            except SubprocessFailure as exc:
                                # spotdl may have resolved the URL before failing — extract it
                                yt_url = self._extract_youtube_url(exc.result)
                                raise

                            # spotdl exited 0 — check if it actually produced a file
                            audio_file = find_first_file(work_dir, suffix=".mp3")
                            if audio_file and audio_file.stat().st_size == 0:
                                audio_file = None

                            # Always try to extract YouTube URL from output (for fallback)
                            if not yt_url:
                                yt_url = self._extract_youtube_url(result)

                            if audio_file:
                                await self._embed_cover_for_file(audio_file, yt_url)
                                dest = self._safe_move(audio_file, playlist_dir)
                                newly_downloaded.append((dest, url))
                            else:
                                raise RuntimeError("spotdl produced no file")
                        except Exception:
                            # Phase 2: yt-dlp fallback (bypasses YouTube Music blocking)
                            if not yt_url:
                                logger.warning("spotdl failed for %s (no YouTube URL extracted), trying ytsearch fallback", url)
                            else:
                                logger.warning("spotdl failed for %s, trying yt-dlp with %s", url, yt_url)
                            await cleanup_paths([work_dir])
                            work_dir = await ensure_clean_directory(
                                settings.temp_dir / f"sp_{task.task_id}_r_{idx}"
                            )
                            try:
                                if yt_url:
                                    # Convert music.youtube.com → youtube.com to avoid Music blocking
                                    dl_url = yt_url.replace("music.youtube.com", "www.youtube.com")
                                    audio_file, _, _ = await self._run_ytdlp_download(task, dl_url, work_dir)
                                else:
                                    search_query = await self._spotify_url_to_search_query(url)
                                    audio_file, _, _, _ = await self._run_search_query_downloads(
                                        task,
                                        work_dir,
                                        query=search_query,
                                        show_progress=False,
                                    )

                                if audio_file:
                                    dest = self._safe_move(audio_file, playlist_dir)
                                    newly_downloaded.append((dest, url))
                                else:
                                    raise RuntimeError("yt-dlp fallback produced no file")
                            except Exception:
                                logger.exception("Track download failed (spotdl + yt-dlp): %s", url)
                                async with _dl_lock:
                                    failed_count += 1
                        finally:
                            await cleanup_paths([work_dir])
                            async with _dl_lock:
                                _dl_done_count += 1
                                done_so_far = cached_count + _dl_done_count
                                await task.update(
                                    build_playlist_status(
                                        DownloadPhase.DOWNLOADING,
                                        done=done_so_far,
                                        total=total,
                                        cached=cached_count,
                                        failed=failed_count,
                                    ),
                                    parse_mode=ParseMode.HTML,
                                )

                await task.update(
                    build_playlist_status(
                        DownloadPhase.DOWNLOADING,
                        done=cached_count,
                        total=total,
                        cached=cached_count,
                    ),
                    parse_mode=ParseMode.HTML,
                )
                await asyncio.gather(
                    *[_download_one(i, url) for i, url in enumerate(uncached_urls)],
                    return_exceptions=True,
                )

            # Cache newly downloaded tracks (with Spotify track ID keys)
            if newly_downloaded and settings.song_cache_channel_id:
                await self._cache_spotify_tracks(app, task, newly_downloaded)

            # ── Build ZIP archive (playlists always deliver as ZIP) ──
            # Download originally-cached tracks from Telegram to disk (parallel)
            stale_spotify_urls: list[str] = []
            if cached_count > 0 and settings.song_cache_channel_id:
                _dl_sem = asyncio.Semaphore(10)
                async def _dl_cached(idx: int, url: str) -> None:
                    if task.cancelled():
                        return
                    async with _dl_sem:
                        cache_key, _ = generate_cache_key(url, InputType.SPOTIFY_TRACK)
                        if not cache_key:
                            return
                        cached = await song_cache.get_cached_song(cache_key)
                        if cached:
                            result = await self._retrieve_cached_track(app, cached, playlist_dir)
                            if result is None:
                                # Cache entry is stale — file gone from Telegram channel
                                await song_cache.invalidate_cache(cache_key)
                                stale_spotify_urls.append(url)
                await asyncio.gather(
                    *[_dl_cached(i, url) for i, url in enumerate(cached_urls)],
                    return_exceptions=True,
                )

            # Re-download stale cached tracks (files deleted from Telegram channel)
            if stale_spotify_urls:
                logger.warning("%d cached tracks have stale Telegram files, re-downloading", len(stale_spotify_urls))
                cached_count -= len(stale_spotify_urls)
                await task.update(
                    build_progress_message(DownloadPhase.DOWNLOADING, details=f"Re-downloading {len(stale_spotify_urls)} stale tracks..."),
                    parse_mode=ParseMode.HTML,
                )
                stale_downloaded: list[tuple[Path, str]] = []
                _stale_sem = asyncio.Semaphore(5)
                async def _stale_one(idx: int, url: str) -> None:
                    nonlocal failed_count
                    if task.cancelled():
                        return
                    async with _stale_sem:
                        if task.cancelled():
                            return
                        work_dir = await ensure_clean_directory(
                            settings.temp_dir / f"sp_{task.task_id}_s{idx}"
                        )
                        try:
                            result = await self._run_spotdl(task, url, work_dir, playlist=False)
                            audio_file = find_first_file(work_dir, suffix=".mp3")
                            if audio_file and audio_file.stat().st_size == 0:
                                audio_file = None
                            if not audio_file:
                                # Retry with alternate audio providers
                                await cleanup_paths([work_dir])
                                work_dir = await ensure_clean_directory(
                                    settings.temp_dir / f"sp_{task.task_id}_s{idx}_r"
                                )
                                result = await self._run_spotdl(
                                    task, url, work_dir, playlist=False,
                                    audio_providers=("youtube", "youtube-music"),
                                )
                                audio_file = find_first_file(work_dir, suffix=".mp3")
                                if audio_file and audio_file.stat().st_size == 0:
                                    audio_file = None
                            if audio_file:
                                yt_url = self._extract_youtube_url(result)
                                await self._embed_cover_for_file(audio_file, yt_url)
                                dest = self._safe_move(audio_file, playlist_dir)
                                stale_downloaded.append((dest, url))
                            else:
                                failed_count += 1
                        except Exception:
                            logger.warning("Stale track re-download failed, retrying with alternate provider: %s", url)
                            await cleanup_paths([work_dir])
                            work_dir = await ensure_clean_directory(
                                settings.temp_dir / f"sp_{task.task_id}_s{idx}_r2"
                            )
                            try:
                                result = await self._run_spotdl(
                                    task, url, work_dir, playlist=False,
                                    audio_providers=("youtube", "youtube-music"),
                                )
                                audio_file = find_first_file(work_dir, suffix=".mp3")
                                if audio_file and audio_file.stat().st_size == 0:
                                    audio_file = None
                                if audio_file:
                                    yt_url = self._extract_youtube_url(result)
                                    await self._embed_cover_for_file(audio_file, yt_url)
                                    dest = self._safe_move(audio_file, playlist_dir)
                                    stale_downloaded.append((dest, url))
                                else:
                                    failed_count += 1
                            except Exception:
                                logger.exception("Stale track re-download failed after retry: %s", url)
                                failed_count += 1
                            finally:
                                await cleanup_paths([work_dir])
                                return
                        finally:
                            await cleanup_paths([work_dir])
                await asyncio.gather(
                    *[_stale_one(i, url) for i, url in enumerate(stale_spotify_urls)],
                    return_exceptions=True,
                )
                # Re-cache stale tracks that were successfully re-downloaded
                if stale_downloaded:
                    await self._cache_spotify_tracks(app, task, stale_downloaded)
                    newly_downloaded.extend(stale_downloaded)

            tracks = list_audio_files(playlist_dir)
            if not tracks:
                raise RuntimeError("Playlist download finished without MP3 files.")

            await task.update(build_progress_message(DownloadPhase.PACKAGING, details=f"{len(tracks)} tracks"), parse_mode=ParseMode.HTML)
            await build_zip(playlist_dir, zip_path)
            zip_chat_id = settings.stream_channel_id or settings.song_cache_channel_id
            upload = await upload_zip_to_storage(app, zip_path, caption=f"Playlist archive for user {task.user_id}", chat_id=zip_chat_id)
            ref = await link_store.create_ref(
                payload={
                    "user_id": task.user_id,
                    "chat_id": zip_chat_id,
                    "message_id": upload.message_id,
                    "file_id": upload.file_id,
                    "file_name": upload.file_name,
                    "file_size": upload.file_size,
                    "bot_index": upload.bot_index,
                },
            )
            link = f"{settings.download_base_url.rstrip('/')}/d/{ref}"
            completion_text, completion_markup = build_playlist_completion(
                track_count=total,
                file_size=upload.file_size,
                download_link=link,
                cached_count=cached_count,
                downloaded_count=len(newly_downloaded),
                failed_count=failed_count,
            )
            task._reply_markup = completion_markup
            await task.update(
                completion_text,
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

    @timed("download_youtube_track")
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
                    logger.warning("Cached file send failed for %s, re-downloading", cache_key, exc_info=True)
                    await song_cache.invalidate_cache(cache_key)

        work_dir = await ensure_clean_directory(settings.temp_dir / f"yt_{task.task_id}")
        thumb_path: Path | None = None
        try:
            await task.update(build_progress_message(DownloadPhase.SEARCHING), parse_mode=ParseMode.HTML)
            duration = int(info.get("duration") or 0)
            audio_file, _, _ = await self._run_ytdlp_download(task, task.request.source, work_dir, timeout=max(duration * 3 + 300, 600))
            thumb_url = info.get("thumbnail")
            if thumb_url:
                thumb_path = await extract_thumbnail_from_url(thumb_url, work_dir / "thumb.jpg")
            if not thumb_path:
                # Fallback: try YouTube thumbnail sizes by video ID
                yt_id = info.get("id")
                if yt_id:
                    thumb_path = await extract_youtube_thumbnail(yt_id, work_dir / "thumb.jpg")
            if not thumb_path and audio_file:
                # Fallback: extract embedded cover art from the MP3 file
                thumb_path = await extract_cover_from_mp3(audio_file, work_dir / "thumb.jpg")
            metadata = await read_audio_metadata(
                audio_file,
                fallback_title=str(info.get("title") or audio_file.stem),
                fallback_artist=str(info.get("uploader") or info.get("channel") or "Unknown Artist"),
            )
            metadata.thumbnail_path = thumb_path
            file_size = audio_file.stat().st_size

            # Cache the downloaded song
            download_url: str | None = None
            cache_file_id: str | None = None
            cache_bot_index: int | None = None
            if cache_key:
                try:
                    await task.update(build_progress_message(DownloadPhase.UPLOADING), parse_mode=ParseMode.HTML)
                    cache_msg_id, audio_file_id, thumb_file_id, upload_bot_index = await song_cache.upload_to_cache_channel(
                        app, audio_file, metadata.title, metadata.artist, metadata.duration, thumb_path,
                        cache_key=cache_key,
                    )
                    # Copy to STREAM_CHANNEL for web download
                    stream_file_id = audio_file_id
                    stream_chat_id = settings.song_cache_channel_id
                    stream_msg_id = cache_msg_id
                    stream_bot_index = upload_bot_index
                    flog_file_id: str | None = None
                    flog_msg_id: int | None = None
                    upload_bot = get_client_by_index(upload_bot_index) or app
                    stream_copy = await copy_audio_to_stream(
                        upload_bot, audio_file_id, metadata.title, metadata.artist, metadata.duration, file_size, thumb_file_id,
                    )
                    if stream_copy:
                        stream_file_id = stream_copy.file_id
                        stream_chat_id = settings.stream_channel_id
                        stream_msg_id = stream_copy.message_id
                        stream_bot_index = stream_copy.bot_index
                        flog_file_id = stream_copy.file_id
                        flog_msg_id = stream_copy.message_id
                    file_name = f"{_build_display_name(metadata.artist, metadata.title)}.mp3"
                    ref = await link_store.create_ref(
                        payload={
                            "user_id": task.user_id,
                            "chat_id": stream_chat_id,
                            "message_id": stream_msg_id,
                            "file_id": stream_file_id,
                            "file_name": file_name,
                            "file_size": file_size,
                            "bot_index": stream_bot_index,
                        },
                    )
                    download_url = f"{settings.download_base_url.rstrip('/')}/d/{ref}"
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
                        cache_message_id=cache_msg_id,
                        flog_file_id=flog_file_id,
                        flog_message_id=flog_msg_id,
                    )
                except Exception:
                    logger.exception("Failed to cache song, sending directly to user")
                else:
                    cache_file_id = audio_file_id
                    cache_bot_index = upload_bot_index

            await self._deliver_audio(app, message, audio_file, metadata, task, download_url=download_url, cache_file_id=cache_file_id, cache_bot_index=cache_bot_index)
            await task.update(build_progress_message(DownloadPhase.COMPLETED), parse_mode=ParseMode.HTML)
        finally:
            await cleanup_paths([work_dir])

    async def _download_youtube_playlist(self, app: Client, message: Message, task: DownloadTask, info: dict[str, Any]) -> None:
        title = sanitize_filename(str(info.get("title") or f"youtube_playlist_{task.user_id}"))
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / f"{title}_{task.task_id}")
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

            # Check cache for each track (batch DB query — no file downloads)
            cached_count = 0
            uncached_entries: list[dict[str, Any]] = []
            cached_entries: list[dict[str, Any]] = []  # entries that were in cache at check time

            if settings.song_cache_channel_id:
                yt_cache_keys = [f"youtube:{e.get('id')}" for e in entries[:total] if e.get("id")]
                cached_map: dict[str, dict[str, Any]] = {}
                if yt_cache_keys:
                    cached_map = await song_cache.get_cached_songs_batch(yt_cache_keys)
                for i, entry in enumerate(entries[:total], 1):
                    if task.cancelled():
                        raise asyncio.CancelledError
                    yt_id = entry.get("id")
                    if yt_id:
                        cache_key = f"youtube:{yt_id}"
                        if cache_key in cached_map:
                            cached_count += 1
                            cached_entries.append(entry)
                        else:
                            uncached_entries.append(entry)
                    else:
                        uncached_entries.append(entry)
                    if i % 5 == 0 or i == total:
                        await task.update(
                            build_playlist_status(
                                DownloadPhase.CHECKING_CACHE,
                                done=i,
                                total=total,
                                cached=cached_count,
                            ),
                            parse_mode=ParseMode.HTML,
                        )
            else:
                uncached_entries = list(entries[:total])

            # Download uncached tracks concurrently (max 5 parallel)
            newly_downloaded: list[tuple[Path, dict[str, Any]]] = []
            failed_count = 0
            _download_concurrency = _YOUTUBE_PLAYLIST_DOWNLOAD_CONCURRENCY
            _dl_sem = asyncio.Semaphore(_download_concurrency)
            _completed_count = 0  # tracks finished (success or fail)
            _progress_lock = asyncio.Lock()

            async def _download_one(entry: dict[str, Any]) -> None:
                nonlocal failed_count, _completed_count
                if task.cancelled():
                    return
                yt_id = entry.get("id")
                if not yt_id:
                    return
                yt_url = f"https://www.youtube.com/watch?v={yt_id}"
                async with _dl_sem:
                    if task.cancelled():
                        return
                    work_dir = await ensure_clean_directory(
                        settings.temp_dir / f"yt_{task.task_id}_{yt_id}"
                    )
                    try:
                        duration = int(entry.get("duration") or 0)
                        audio_file, thumb_url, _ = await self._run_ytdlp_download(
                            task, yt_url, work_dir, timeout=max(duration * 3 + 300, 600),
                        )
                        entry["_thumb_url"] = thumb_url
                        dest = self._safe_move(audio_file, playlist_dir)
                        newly_downloaded.append((dest, entry))
                    except Exception:
                        logger.exception("Failed to download track: %s", entry.get("title"))
                        async with _progress_lock:
                            failed_count += 1
                    finally:
                        await cleanup_paths([work_dir])
                        async with _progress_lock:
                            _completed_count += 1
                            done = cached_count + _completed_count
                            if _completed_count % 2 == 0 or _completed_count == len(uncached_entries):
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

            if uncached_entries:
                # Initial progress update
                await task.update(
                    build_playlist_status(
                        DownloadPhase.DOWNLOADING,
                        done=cached_count,
                        total=total,
                        cached=cached_count,
                    ),
                    parse_mode=ParseMode.HTML,
                )
                await asyncio.gather(*[_download_one(e) for e in uncached_entries])

            # Cache newly downloaded tracks
            if newly_downloaded:
                yt_entries_map: dict[int, dict[str, Any]] = {}
                for idx, entry in enumerate(uncached_entries, 1):
                    yt_entries_map[idx] = entry
                await self._cache_new_tracks(app, task, newly_downloaded, yt_entries_map)

            # ── Build ZIP archive (playlists always deliver as ZIP) ──
            # Download originally-cached tracks from Telegram to disk (parallel)
            stale_yt_entries: list[dict[str, Any]] = []
            if cached_count > 0 and settings.song_cache_channel_id:
                _yt_dl_sem = asyncio.Semaphore(10)
                async def _yt_dl_cached(idx: int, entry: dict[str, Any]) -> None:
                    if task.cancelled():
                        return
                    async with _yt_dl_sem:
                        yt_id = entry.get("id")
                        if not yt_id:
                            return
                        cache_key = f"youtube:{yt_id}"
                        cached = await song_cache.get_cached_song(cache_key)
                        if cached:
                            result = await self._retrieve_cached_track(app, cached, playlist_dir)
                            if result is None:
                                # Cache entry is stale — file gone from Telegram channel
                                await song_cache.invalidate_cache(cache_key)
                                stale_yt_entries.append(entry)
                await asyncio.gather(
                    *[_yt_dl_cached(i, e) for i, e in enumerate(cached_entries)],
                    return_exceptions=True,
                )

            # Re-download stale cached tracks (files deleted from Telegram channel)
            if stale_yt_entries:
                logger.warning("%d cached tracks have stale Telegram files, re-downloading", len(stale_yt_entries))
                cached_count -= len(stale_yt_entries)
                await task.update(
                    build_progress_message(DownloadPhase.DOWNLOADING, details=f"Re-downloading {len(stale_yt_entries)} stale tracks..."),
                    parse_mode=ParseMode.HTML,
                )
                stale_yt_downloaded: list[tuple[Path, dict[str, Any]]] = []
                _yt_stale_sem = asyncio.Semaphore(_download_concurrency)
                async def _yt_stale_one(entry: dict[str, Any]) -> None:
                    nonlocal failed_count
                    if task.cancelled():
                        return
                    yt_id = entry.get("id")
                    if not yt_id:
                        return
                    yt_url = f"https://www.youtube.com/watch?v={yt_id}"
                    async with _yt_stale_sem:
                        if task.cancelled():
                            return
                        work_dir = await ensure_clean_directory(
                            settings.temp_dir / f"yt_{task.task_id}_s_{yt_id}"
                        )
                        try:
                            duration = int(entry.get("duration") or 0)
                            audio_file, thumb_url, _ = await self._run_ytdlp_download(
                                task, yt_url, work_dir, timeout=max(duration * 3 + 300, 600),
                            )
                            entry["_thumb_url"] = thumb_url
                            dest = self._safe_move(audio_file, playlist_dir)
                            stale_yt_downloaded.append((dest, entry))
                        except Exception:
                            logger.exception("Stale track re-download failed: %s", entry.get("title"))
                            failed_count += 1
                        finally:
                            await cleanup_paths([work_dir])
                await asyncio.gather(*[_yt_stale_one(e) for e in stale_yt_entries])
                # Re-cache stale tracks that were successfully re-downloaded
                if stale_yt_downloaded:
                    stale_entries_map = {i: e for i, e in enumerate(stale_yt_entries, 1)}
                    await self._cache_new_tracks(app, task, stale_yt_downloaded, stale_entries_map)
                    newly_downloaded.extend(stale_yt_downloaded)

            tracks = list_audio_files(playlist_dir)
            if not tracks:
                raise RuntimeError("Playlist download finished but no MP3 files were found.")

            await task.update(build_progress_message(DownloadPhase.PACKAGING, details=f"{len(tracks)} tracks"), parse_mode=ParseMode.HTML)
            await build_zip(playlist_dir, zip_path)
            zip_chat_id = settings.stream_channel_id or settings.song_cache_channel_id
            upload = await upload_zip_to_storage(app, zip_path, caption=f"YouTube playlist archive for user {task.user_id}", chat_id=zip_chat_id)
            link = await link_store.create_ref(
                payload={
                    "user_id": task.user_id,
                    "chat_id": zip_chat_id,
                    "message_id": upload.message_id,
                    "file_id": upload.file_id,
                    "file_name": upload.file_name,
                    "file_size": upload.file_size,
                    "bot_index": upload.bot_index,
                },
            )
            link = f"{settings.download_base_url.rstrip('/')}/d/{link}"
            completion_text, completion_markup = build_playlist_completion(
                track_count=len(tracks),
                file_size=upload.file_size,
                download_link=link,
                cached_count=cached_count,
                downloaded_count=len(newly_downloaded),
                failed_count=failed_count,
            )
            task._reply_markup = completion_markup
            await task.update(
                completion_text,
                parse_mode=ParseMode.HTML,
            )
        finally:
            await cleanup_paths([playlist_dir, zip_path])

    @staticmethod
    def _safe_move(src: Path, dest_dir: Path) -> Path:
        """Move src into dest_dir, appending a numeric suffix if the target exists."""
        dest = dest_dir / src.name
        if not dest.exists():
            shutil.move(str(src), str(dest))
            return dest
        n = 2
        while True:
            candidate = dest_dir / f"{src.stem} ({n}){src.suffix}"
            if not candidate.exists():
                shutil.move(str(src), str(candidate))
                return candidate
            n += 1

    async def _retrieve_cached_track(self, app: Client, cached: dict[str, Any], dest_dir: Path) -> Path | None:
        """Download a cached song from the cache channel to a local file."""
        bot_idx = int(cached.get("bot_index", 0) or 0)
        owner = get_client_by_index(bot_idx)
        if owner is None or not owner.is_connected:
            owner = app
        try:
            file_id = cached["telegram_file_id"]
            title = cached.get("title", "Unknown")
            artist = cached.get("artist", "Unknown Artist")
            file_name = sanitize_filename(f"{_build_display_name(artist, title)}.mp3")
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
            await owner.download_media(file_id, file_name=str(dest_path))
            if dest_path.exists() and dest_path.stat().st_size > 0:
                return dest_path
        except Exception as e:
            err_name = type(e).__name__
            if err_name == "FileReferenceExpired":
                # Re-fetch message from cache channel to get fresh file reference
                cache_msg_id = cached.get("cache_message_id")
                if cache_msg_id:
                    try:
                        msgs = await owner.get_messages(
                            settings.song_cache_channel_id, cache_msg_id,
                        )
                        if msgs and msgs.audio:
                            fresh_file_id = msgs.audio.file_id
                            # Update cache with fresh file_id
                            cached["telegram_file_id"] = fresh_file_id
                            _song_cache.set(cached.get("cache_key", ""), cached, ttl=300.0)
                            db = get_database()
                            await db["cached_songs"].update_one(
                                {"cache_key": cached.get("cache_key")},
                                {"$set": {"telegram_file_id": fresh_file_id}},
                            )
                            logger.info("Refreshed file_reference for %s - %s", artist, title)
                            # Retry download with fresh file_id
                            await owner.download_media(fresh_file_id, file_name=str(dest_path))
                            if dest_path.exists() and dest_path.stat().st_size > 0:
                                return dest_path
                    except Exception:
                        logger.debug("Failed to refresh file_reference for %s - %s", artist, title, exc_info=True)
                logger.warning("FileReferenceExpired and could not refresh: %s - %s", artist, title)
                return None
            logger.exception("Failed to retrieve cached track: %s - %s", artist, title)
        return None

    _SPOTIFY_PLAYLIST_RE = re.compile(r"/playlist/([A-Za-z0-9]+)")
    _spotify_api_token: dict[str, Any] = {}  # {"token": str, "expires_at": float}

    async def _get_spotify_api_token(self) -> str | None:
        """Get a Spotify API access token using client credentials flow."""
        if not settings.spotify_client_id or not settings.spotify_client_secret:
            return None
        now = time.monotonic()
        cached = self._spotify_api_token
        if cached.get("token") and cached.get("expires_at", 0) > now + 60:
            return cached["token"]
        try:
            import base64
            import urllib.request
            import urllib.parse
            credentials = base64.b64encode(
                f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
            ).decode()
            req = urllib.request.Request(
                "https://accounts.spotify.com/api/token",
                data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
            )
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
            data = json.loads(resp.read())
            token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._spotify_api_token = {"token": token, "expires_at": now + expires_in}
            return token
        except Exception:
            logger.warning("Failed to get Spotify API token", exc_info=True)
        return None

    async def _get_spotify_playlist_tracks_via_api(self, playlist_id: str) -> list[str]:
        """Fetch playlist track URLs directly from Spotify Web API."""
        token = await self._get_spotify_api_token()
        if not token:
            logger.warning("Spotify API: failed to obtain token (client_id set=%s, secret set=%s)",
                           bool(settings.spotify_client_id), bool(settings.spotify_client_secret))
            return []
        try:
            import urllib.request
            from urllib.error import HTTPError
            headers = {"Authorization": f"Bearer {token}"}
            urls: list[str] = []
            api_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100"
            loop = asyncio.get_running_loop()
            page = 0
            while api_url:
                page += 1
                req = urllib.request.Request(api_url, headers=headers)
                try:
                    resp = await loop.run_in_executor(None, lambda r=req: urllib.request.urlopen(r, timeout=15))
                except HTTPError as e:
                    body = e.read().decode("utf-8", errors="replace")[:500]
                    logger.warning("Spotify API HTTP %d for page %d: %s", e.code, page, body)
                    if e.code == 401:
                        # Token expired — refresh and retry once
                        self._spotify_api_token = {}
                        token = await self._get_spotify_api_token()
                        if token:
                            headers["Authorization"] = f"Bearer {token}"
                            req = urllib.request.Request(api_url, headers=headers)
                            resp = await loop.run_in_executor(None, lambda r=req: urllib.request.urlopen(r, timeout=15))
                        else:
                            break
                    elif e.code == 429:
                        retry_after = int(e.headers.get("Retry-After", "3"))
                        logger.info("Spotify API rate limited, retrying in %ds", retry_after)
                        await asyncio.sleep(retry_after)
                        req = urllib.request.Request(api_url, headers=headers)
                        resp = await loop.run_in_executor(None, lambda r=req: urllib.request.urlopen(r, timeout=15))
                    else:
                        break
                data = json.loads(resp.read())
                items = data.get("items", [])
                for item in items:
                    track = item.get("track")
                    if track and track.get("id"):
                        url = f"https://open.spotify.com/track/{track['id']}"
                        urls.append(url)
                        self._remember_spotify_track_search_query(url, track)
                logger.debug("Spotify API page %d: %d items (%d total)", page, len(items), len(urls))
                api_url = data.get("next")
            return urls
        except Exception:
            logger.warning("Failed to fetch playlist from Spotify API", exc_info=True)
            return []

    async def _get_spotify_playlist_track_urls(self, task: DownloadTask, out_dir: Path) -> list[str]:
        """Extract individual track URLs from a Spotify playlist.

        Tries Spotify Web API first (fast, 1-2 requests), falls back to spotdl save.
        """
        # Method 1: Spotify Web API (fast — single paginated request, no YouTube needed)
        playlist_match = self._SPOTIFY_PLAYLIST_RE.search(task.request.source)
        if playlist_match and settings.spotify_client_id and settings.spotify_client_secret:
            playlist_id = playlist_match.group(1)
            urls = await self._get_spotify_playlist_tracks_via_api(playlist_id)
            if urls:
                logger.info("Spotify Web API returned %d tracks for %s", len(urls), playlist_id)
                return urls
            logger.warning("Spotify Web API returned no tracks, falling back to spotdl save")

        # Method 2: spotdl save (slow — resolves each track via YouTube Music)
        save_file = out_dir / "_tracks.spotdl"
        cmd = [
            _SPOTDL_BIN,
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
            await self._run_subprocess(task, cmd, "spotdl-save")
        except SubprocessFailure:
            logger.warning("spotdl save failed")
            return []
        if save_file.exists():
            try:
                data = _json_loads(save_file.read_text())
                urls: list[str] = []
                for song in data:
                    if not isinstance(song, dict) or "url" not in song:
                        continue
                    url = str(song["url"])
                    urls.append(url)
                    self._remember_spotify_track_search_query(url, song)
                return urls
            except (json.JSONDecodeError, KeyError):
                logger.warning("Failed to parse spotdl save output")
            finally:
                save_file.unlink(missing_ok=True)
        return []

    def _canonicalize_spotify_track_url(self, spotify_url: str) -> str | None:
        track_match = self._SPOTIFY_TRACK_RE.search(spotify_url)
        if not track_match:
            return None
        return f"https://open.spotify.com/track/{track_match.group(1)}"

    def _build_spotify_track_search_query(self, track_data: dict[str, Any]) -> str | None:
        title = str(track_data.get("name") or track_data.get("title") or "").strip()
        raw_artists = track_data.get("artists") or track_data.get("artist")
        artists: list[str] = []
        if isinstance(raw_artists, list):
            for artist in raw_artists:
                if isinstance(artist, dict):
                    name = str(artist.get("name") or "").strip()
                else:
                    name = str(artist).strip()
                if name:
                    artists.append(name)
        elif raw_artists:
            artists = [str(raw_artists).strip()]

        if artists and title:
            return f"{', '.join(artists)} - {title}"
        if title:
            return title
        return None

    def _remember_spotify_track_search_query(self, spotify_url: str, track_data: dict[str, Any]) -> None:
        canonical_url = self._canonicalize_spotify_track_url(spotify_url)
        if not canonical_url:
            return
        query = self._build_spotify_track_search_query(track_data)
        if not query:
            return
        self._spotify_track_search_queries[canonical_url] = query

    async def _cache_new_tracks(
        self,
        app: Client,
        task: DownloadTask,
        tracks: list[tuple[Path, dict[str, Any]]],
        yt_entries: dict[int, dict[str, Any]] | None = None,
    ) -> int:
        """Cache newly downloaded YouTube playlist tracks with parallel uploads + bulk DB writes.

        Returns count cached.
        """
        if not settings.song_cache_channel_id or not tracks:
            return 0

        total = len(tracks)
        sem = asyncio.Semaphore(_UPLOAD_SEMAPHORE)
        # (index, cache_key, cache_msg_id, audio_file_id, thumb_file_id, file_size, metadata, thumb_path)
        upload_results: list[tuple[int, str, int, str, str | None, int, Any, Path | None]] = []

        async def _upload_one(idx: int, track_path: Path, entry: dict[str, Any]) -> None:
            if task.cancelled():
                return
            async with sem:
                if task.cancelled():
                    return
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
                        return

                    thumb_url = entry.get("_thumb_url") or (yt_entries.get(idx, {}).get("thumbnail") if yt_entries else None)
                    thumb_path: Path | None = None
                    if thumb_url:
                        try:
                            thumb_path = await extract_thumbnail_from_url(
                                thumb_url, track_path.parent / f"_cthumb_{idx}.jpg",
                            )
                        except Exception:
                            logger.debug("Thumbnail extraction failed for track %d", idx)

                    cache_msg_id, audio_file_id, thumb_file_id, upload_bot_index = await song_cache.upload_to_cache_channel(
                        app, track_path, metadata.title, metadata.artist, metadata.duration, thumb_path,
                        cache_key=cache_key,
                    )
                    file_size = track_path.stat().st_size
                    upload_results.append((idx, cache_key, cache_msg_id, audio_file_id, thumb_file_id, file_size, metadata, thumb_path, upload_bot_index))
                except Exception:
                    logger.exception("Failed to upload track to cache: %s", track_path.name)

        # Phase A: Parallel upload to SONG_CACHE
        await asyncio.gather(*[_upload_one(i, tp, entry) for i, (tp, entry) in enumerate(tracks, 1)],
                             return_exceptions=True)

        if not upload_results or task.cancelled():
            return 0

        # Phase B: Bulk ref creation
        ref_payloads: list[dict[str, Any]] = []
        for _, cache_key, cache_msg_id, audio_file_id, _, file_size, metadata, _, bot_idx in upload_results:
            ref_payloads.append({
                "user_id": task.user_id,
                "chat_id": settings.song_cache_channel_id,
                "message_id": cache_msg_id,
                "file_id": audio_file_id,
                "file_name": f"{_build_display_name(metadata.artist, metadata.title)}.mp3",
                "file_size": file_size,
                "bot_index": bot_idx,
            })

        refs = await link_store.create_refs_bulk(ref_payloads)

        # Phase C: Bulk cache metadata write
        cache_docs: list[dict[str, Any]] = []
        for i, (_, cache_key, cache_msg_id, audio_file_id, thumb_file_id, file_size, metadata, thumb_path, bot_idx) in enumerate(upload_results):
            download_url = f"{settings.download_base_url.rstrip('/')}/d/{refs[i]}" if i < len(refs) else None
            cache_docs.append({
                "cache_key": cache_key,
                "cache_key_type": "youtube",
                "title": metadata.title,
                "artist": metadata.artist,
                "duration": metadata.duration,
                "file_size": file_size,
                "telegram_file_id": audio_file_id,
                "thumbnail_file_id": thumb_file_id,
                "download_link": download_url,
                "cache_message_id": cache_msg_id,
                "created_at": datetime.now(timezone.utc),
                "bot_index": bot_idx,
            })
            if thumb_path:
                thumb_path.unlink(missing_ok=True)

        cached_count = await song_cache.cache_songs_bulk(cache_docs)

        if cached_count:
            logger.info("Cached %d/%d new tracks (parallel)", cached_count, total)
        return cached_count

    async def _cache_spotify_tracks(
        self,
        app: Client,
        task: DownloadTask,
        tracks: list[tuple[Path, str]],
    ) -> int:
        """Cache newly downloaded Spotify playlist tracks with parallel uploads + bulk DB writes.

        Uses Spotify track IDs as cache keys.
        """
        if not settings.song_cache_channel_id or not tracks:
            return 0

        total = len(tracks)
        sem = asyncio.Semaphore(_UPLOAD_SEMAPHORE)
        upload_results: list[tuple[int, str, tuple[int, str, str | None], int, str, Any, Any]] = []

        async def _upload_one(idx: int, track_path: Path, spotify_url: str) -> None:
            if task.cancelled():
                return
            async with sem:
                if task.cancelled():
                    return
                try:
                    metadata = await read_audio_metadata(
                        track_path,
                        fallback_title=track_path.stem,
                        fallback_artist="Unknown Artist",
                    )
                    cache_key, _ = generate_cache_key(spotify_url, InputType.SPOTIFY_TRACK)
                    if not cache_key or await song_cache.get_cached_song(cache_key):
                        return

                    cache_msg_id, audio_file_id, thumb_file_id, upload_bot_index = await song_cache.upload_to_cache_channel(
                        app, track_path, metadata.title, metadata.artist, metadata.duration,
                        cache_key=cache_key,
                    )
                    file_size = track_path.stat().st_size
                    upload_results.append((idx, spotify_url, (cache_msg_id, audio_file_id, thumb_file_id), file_size, cache_key, metadata, thumb_file_id, upload_bot_index))
                except Exception:
                    logger.exception("Failed to upload Spotify track to cache: %s", spotify_url)

        # Phase A: Parallel upload to SONG_CACHE
        await asyncio.gather(*[_upload_one(i, tp, url) for i, (tp, url) in enumerate(tracks, 1)],
                             return_exceptions=True)

        if not upload_results or task.cancelled():
            return 0

        # Phase B: Bulk ref creation
        ref_payloads: list[dict[str, Any]] = []
        for idx, spotify_url, (cache_msg_id, audio_file_id, _), file_size, cache_key, metadata, _, bot_idx in upload_results:
            ref_payloads.append({
                "user_id": task.user_id,
                "chat_id": settings.song_cache_channel_id,
                "message_id": cache_msg_id,
                "file_id": audio_file_id,
                "file_name": f"{_build_display_name(metadata.artist, metadata.title)}.mp3",
                "file_size": file_size,
                "bot_index": bot_idx,
            })

        refs = await link_store.create_refs_bulk(ref_payloads)

        # Phase C: Bulk cache metadata write
        cache_docs: list[dict[str, Any]] = []
        for i, (idx, spotify_url, (cache_msg_id, audio_file_id, thumb_file_id), file_size, cache_key, metadata, _, bot_idx) in enumerate(upload_results):
            download_url = f"{settings.download_base_url.rstrip('/')}/d/{refs[i]}" if i < len(refs) else None
            cache_docs.append({
                "cache_key": cache_key,
                "cache_key_type": "spotify",
                "title": metadata.title,
                "artist": metadata.artist,
                "duration": metadata.duration,
                "file_size": file_size,
                "telegram_file_id": audio_file_id,
                "thumbnail_file_id": thumb_file_id,
                "download_link": download_url,
                "cache_message_id": cache_msg_id,
                "created_at": datetime.now(timezone.utc),
                "bot_index": bot_idx,
            })

        cached_count = await song_cache.cache_songs_bulk(cache_docs)

        if cached_count:
            logger.info("Cached %d/%d Spotify tracks (parallel)", cached_count, total)
        return cached_count

    async def _spotify_url_to_search_query(self, spotify_url: str) -> str:
        """Convert a Spotify track URL to a search query using the Spotify Web API.

        Returns 'Artist - Title' for better YouTube search results.
        Falls back to the raw URL if the API is unavailable.
        """
        canonical_url = self._canonicalize_spotify_track_url(spotify_url)
        if canonical_url:
            cached_query = self._spotify_track_search_queries.get(canonical_url)
            if cached_query:
                return cached_query

        track_match = self._SPOTIFY_TRACK_RE.search(spotify_url)
        if not track_match:
            return spotify_url
        track_id = track_match.group(1)
        try:
            token = await self._get_spotify_api_token()
            if token:
                import urllib.request
                api_url = f"https://api.spotify.com/v1/tracks/{track_id}"
                loop = asyncio.get_running_loop()
                req = urllib.request.Request(api_url, headers={"Authorization": f"Bearer {token}"})
                resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
                data = json.loads(resp.read())
                artists = ", ".join(a["name"] for a in data.get("artists", []))
                title = data.get("name", "")
                if artists and title:
                    query = f"{artists} - {title}"
                    if canonical_url:
                        self._spotify_track_search_queries[canonical_url] = query
                    return query
        except Exception:
            logger.debug("Spotify track lookup failed for %s", track_id, exc_info=True)
        return spotify_url

    _SPOTIFY_TRACK_RE = re.compile(r"/track/([A-Za-z0-9]+)")

    async def _resolve_search_and_check_cache(self, app: Client, message: Message, task: DownloadTask) -> str | None:
        """Resolve a search query to a YouTube URL, checking cache.

        Returns the YouTube URL if not cached (caller should download).
        Returns None if cache hit (song already sent to user).
        Returns _RESOLVE_FAILED if resolution failed entirely.
        """
        try:
            search_info = await search_youtube_music(task.request.source)
            if search_info is None:
                return _RESOLVE_FAILED  # type: ignore[return-value]
            entry = first_valid_entry(search_info)
            yt_id = extract_youtube_id(entry)
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

    async def _run_search_query_downloads(
        self,
        task: DownloadTask,
        out_dir: Path,
        attempted: set[str] | None = None,
        query: str | None = None,
        show_progress: bool = True,
    ) -> tuple[Path | None, str | None, dict[str, Any] | None, set[str]]:
        attempted_queries = attempted if attempted is not None else set()
        base_query = (query or task.request.source).strip()
        search_queries = build_search_candidates(base_query)
        if not search_queries and base_query:
            search_queries = [base_query]
        total = len(search_queries)

        for index, candidate in enumerate(search_queries, start=1):
            key = candidate.casefold()
            if key in attempted_queries:
                continue

            attempted_queries.add(key)
            await ensure_clean_directory(out_dir)
            if show_progress:
                details = None if index == 1 else f"Trying alternative search {index}/{total}..."
                await task.update(build_progress_message(DownloadPhase.SEARCHING, details=details), parse_mode=ParseMode.HTML)
            logger.info("Trying search variant %d/%d for %r: %s", index, total, base_query, candidate)

            try:
                audio_file, thumb_url, yt_info = await self._run_ytdlp_download(
                    task,
                    f"ytsearch1:{candidate}",
                    out_dir,
                )
            except Exception:
                logger.exception("ytsearch variant failed for %r using %r", base_query, candidate)
                continue

            if audio_file:
                return audio_file, thumb_url, yt_info, attempted_queries

        return None, None, None, attempted_queries

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
        client_id = id(app)
        cached = _bot_usernames.get(client_id)
        if cached:
            return cached
        try:
            me = await app.get_me()
            if me and me.username:
                _bot_usernames[client_id] = me.username
                return me.username
        except Exception:
            logger.debug("Failed to resolve bot username")
        return None

    async def _send_cached_audio(self, app: Client, message: Message, cached: dict[str, Any], task: DownloadTask) -> None:
        """Send a cached song to the user using the stored Telegram file_id.

        Free users receive a download link only (no audio in Telegram).
        Pro users receive audio directly in Telegram + download link.
        """
        current_bot_index = get_client_index(app)
        owner_bot_index = int(cached.get("bot_index", 0) or 0)
        username = await self._get_bot_username(app)
        caption = build_audio_caption(
            title=cached["title"],
            artist=cached["artist"],
            duration=cached["duration"],
        )

        # Always create a new STREAM_CHANNEL copy per request (per-request per-file)
        stream_file_id = cached["telegram_file_id"]
        stream_chat_id = settings.song_cache_channel_id
        stream_msg_id = cached.get("cache_message_id", 0)
        stream_bot_index = cached.get("bot_index", 0)
        if settings.stream_channel_id:
            cache_bot = get_client_by_index(stream_bot_index) or app
            stream_copy = await copy_audio_to_stream(
                cache_bot, stream_file_id, cached["title"], cached["artist"],
                cached["duration"], cached["file_size"],
            )
            if stream_copy:
                stream_file_id = stream_copy.file_id
                stream_chat_id = settings.stream_channel_id
                stream_msg_id = stream_copy.message_id
                stream_bot_index = stream_copy.bot_index

        ref = await link_store.create_ref(
            payload={
                "user_id": message.from_user.id,
                "chat_id": stream_chat_id,
                "message_id": stream_msg_id,
                "file_id": stream_file_id,
                "file_name": f"{_build_display_name(cached['artist'], cached['title'])}.mp3",
                "file_size": cached["file_size"],
                "bot_index": stream_bot_index,
            },
        )
        download_url = f"{settings.download_base_url.rstrip('/')}/d/{ref}"

        # Free users: send download link only
        if not task.is_pro:
            text, markup = build_free_delivery_message(
                title=cached["title"],
                artist=cached["artist"],
                duration=cached["duration"],
                download_url=download_url,
            )
            await app.send_message(
                chat_id=message.chat.id,
                text=text,
                disable_web_page_preview=True,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return

        # Pro users: send audio with download link
        audio_markup = build_audio_keyboard(username, download_url=download_url) if username else None
        if owner_bot_index == current_bot_index:
            try:
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
                )
                return
            except Exception:
                logger.warning(
                    "Cached file_id send failed for %s on bot %d, falling back to cached retrieval",
                    cached.get("cache_key"),
                    current_bot_index,
                    exc_info=True,
                )

        work_dir = await ensure_clean_directory(settings.temp_dir / f"cache_{task.task_id}")
        try:
            cached_file = await self._retrieve_cached_track(app, cached, work_dir)
            if not cached_file:
                raise RuntimeError("Failed to retrieve cached track for delivery.")
            await app.send_audio(
                chat_id=message.chat.id,
                audio=str(cached_file),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=task.original_message_id,
                reply_markup=audio_markup,
                title=cached["title"],
                performer=cached["artist"],
                duration=cached["duration"],
            )
        finally:
            await cleanup_paths([work_dir])

    async def _send_large_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any, task: DownloadTask) -> None:
        """Upload large audio (>50MB) to private channel and send download link."""
        upload = await upload_zip_to_storage(app, audio_file, caption=f"{metadata.title} - {metadata.artist}")
        link = await link_store.create_link(
            user_id=task.user_id,
            payload={
                "chat_id": settings.stream_channel_id,
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

    async def _deliver_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any, task: DownloadTask, download_url: str | None = None, cache_file_id: str | None = None, cache_bot_index: int | None = None) -> None:
        """Send audio directly if under 2GB, otherwise upload to channel and send link.

        Free users receive a download link only (no audio in Telegram).
        Pro users receive audio directly in Telegram + download link.
        When cache_file_id is provided, uses server-side copy instead of re-uploading.
        """
        file_size = audio_file.stat().st_size

        # Free users get link-only delivery when download URL is available
        if download_url and not task.is_pro:
            text, markup = build_free_delivery_message(
                title=metadata.title,
                artist=metadata.artist,
                duration=metadata.duration,
                download_url=download_url,
            )
            await app.send_message(
                chat_id=message.chat.id,
                text=text,
                disable_web_page_preview=True,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return

        # Pro users (or Free without download URL) get audio delivery
        if file_size <= _TELEGRAM_BOT_UPLOAD_LIMIT:
            username = await self._get_bot_username(app)
            audio_markup = build_audio_keyboard(username, download_url=download_url) if username else None
            current_bot_index = get_client_index(app)

            # Fast path: use cached file_id for server-side copy (no re-upload)
            if cache_file_id is not None and cache_bot_index is not None and cache_bot_index == current_bot_index:
                cache_bot = get_client_by_index(cache_bot_index)
                if cache_bot and cache_bot.is_connected:
                    caption = build_audio_caption(
                        title=metadata.title,
                        artist=metadata.artist,
                        duration=metadata.duration,
                    )
                    try:
                        await cache_bot.send_audio(
                            chat_id=message.chat.id,
                            audio=cache_file_id,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_to_message_id=task.original_message_id,
                            reply_markup=audio_markup,
                            title=metadata.title,
                            performer=metadata.artist,
                            duration=metadata.duration,
                        )
                        return
                    except Exception:
                        logger.warning("Cache file_id send failed, falling back to local upload", exc_info=True)
            elif cache_file_id is not None and cache_bot_index is not None:
                logger.info(
                    "Skipping cross-bot cached file_id delivery for %s - %s (owner bot %d, current bot %d)",
                    metadata.artist,
                    metadata.title,
                    cache_bot_index,
                    current_bot_index,
                )

            # Fallback: upload from local file
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
            _SPOTDL_BIN,
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
        cached_count: int = 0,
        audio_providers: tuple[str, ...] = ("youtube-music", "youtube"),
    ) -> SubprocessResult:
        """Run spotdl download with multiple URLs in a single process to avoid per-track rate limiting."""
        cmd = [
            _SPOTDL_BIN,
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
        extra = {"cached": cached_count} if cached_count > 0 else {}
        return await self._run_subprocess(task, cmd, "spotdl", total_tracks=total_tracks, done_offset=done_offset, extra_state=extra)

    _FIRST_OUTPUT_TIMEOUT: int = 60
    _STALL_TIMEOUT: int = 90

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        """Kill the entire process group to ensure child processes are also terminated."""
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
        extra_state: dict[str, int] | None = None,
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
            {"total": total_tracks, "done": done_offset, **(extra_state or {})} if name in ("spotdl", "spotdl-save") else {}
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
            if process.returncode is None:
                self._kill_process_group(process)
                await process.wait()

    def _map_subprocess_progress(self, name: str, text: str, spotdl_state: dict[str, int] | None = None) -> str | None:
        lowered = text.lower()

        # spotdl-save is metadata-only lookup — show reading progress per track
        if name == "spotdl-save":
            if "found" in lowered and "song" in lowered:
                total_match = re.search(r"found\s+(\d+)\s+song", lowered)
                if total_match and spotdl_state is not None:
                    count = int(total_match.group(1))
                    spotdl_state["total"] = count
                    return build_progress_message(DownloadPhase.SEARCHING, details=f"Found {count} tracks")
            if "rate" in lowered and "limit" in lowered:
                return None
            # Track per-song metadata lookup progress
            if spotdl_state is not None and spotdl_state.get("total", 0) > 0:
                if "download" in lowered or "processing" in lowered or "saved" in lowered:
                    spotdl_state["done"] = min(spotdl_state.get("done", 0) + 1, spotdl_state["total"])
                    return build_progress_message(
                        DownloadPhase.SEARCHING,
                        details=f"Reading {spotdl_state['done']}/{spotdl_state['total']}...",
                    )
            return None

        if name != "spotdl":
            if "download" in lowered or "converting" in lowered or "processing" in lowered:
                return build_progress_message(DownloadPhase.DOWNLOADING)
            return None

        if "processing query" in lowered:
            return build_progress_message(DownloadPhase.SEARCHING)

        # Track total song count from "Found X songs" line — show static text, not a counter
        # spotdl save doesn't emit per-track output, so a counter would jump 0/64 → 64/64
        if spotdl_state is not None:
            total_match = re.search(r"found\s+(\d+)\s+song", lowered)
            if total_match:
                total = int(total_match.group(1))
                spotdl_state["total"] = total
                return build_progress_message(DownloadPhase.SEARCHING, details=f"Found {total} tracks, looking up...")

        if "saved" in lowered and "song" in lowered:
            saved_match = re.search(r"saved\s+(\d+)\s+song", lowered)
            if saved_match and spotdl_state is not None:
                spotdl_state["done"] += int(saved_match.group(1))
                return None  # No UI update — next phase (CHECKING_CACHE) has real per-track progress

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
                    done = min(done, total)  # prevent 65/64 overflow
                    cached = spotdl_state.get("cached", 0)
                    downloading = max(0, done - cached)
                    return build_playlist_status(
                        DownloadPhase.DOWNLOADING, done=done, total=total,
                        cached=cached, downloading=downloading,
                    )
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
            match = re.search(r"https?://(?:www\.|music\.)?(?:youtube\.com|youtu\.be)\S+", text)
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
            match = re.search(r"https?://(?:www\.|music\.)?(?:youtube\.com|youtu\.be)\S+", text)
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
            uid = os.urandom(4).hex()
            cover = await extract_thumbnail_from_url(cover_url, mp3_path.parent / f"_cover_{mp3_path.stem}_{uid}.jpg")
            if not cover:
                # Fallback: try multiple YouTube thumbnail sizes if this is a YouTube URL
                yt_id_match = re.search(r"i\.ytimg\.com/vi/([A-Za-z0-9_-]{11})", cover_url)
                if yt_id_match:
                    cover = await extract_youtube_thumbnail(yt_id_match.group(1), mp3_path.parent / f"_cover_{mp3_path.stem}_{uid}.jpg")
            if not cover:
                return
            temp_path = mp3_path.parent / f"{mp3_path.stem}_{uid}._cover_embed.tmp.mp3"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", str(mp3_path),
                "-i", str(cover),
                "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-metadata:s:v", "comment=Cover (front)",
                "-c:a", "copy",
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
                logger.warning("FFmpeg cover art embedding failed for %s (rc=%d)", mp3_path.name, proc.returncode)
        except Exception:
            logger.warning("Failed to embed cover art in %s", mp3_path.name, exc_info=True)

    async def _embed_cover_for_file(self, mp3_path: Path, yt_url: str | None) -> None:
        """Extract YouTube video ID from URL and embed cover art."""
        if not yt_url or not mp3_path.exists():
            return
        yt_id_match = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", yt_url)
        if yt_id_match:
            thumb_url = f"https://i.ytimg.com/vi/{yt_id_match.group(1)}/maxresdefault.jpg"
            await self._embed_cover_in_mp3(mp3_path, thumb_url)

    _MAX_CONCURRENT_EMBEDS = 5

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

    @timed("convert_to_mp3")
    async def _convert_to_mp3(self, input_path: Path, task: DownloadTask, timeout: float | None = None, *, title: str | None = None, artist: str | None = None, cover_path: Path | None = None) -> Path:
        """Convert an audio file to MP3 using FFmpeg as a subprocess with a timeout."""
        duration = await self._validate_audio_file(input_path)
        # Dynamic timeout: scale with audio duration (3x real-time + 300s overhead, capped at 1800s)
        if timeout is None:
            timeout = min(max(int(duration) * 3 + 300, _CONVERSION_TIMEOUT_BASE), 1800)
        output_path = input_path.with_suffix(".mp3")
        text = build_progress_message(DownloadPhase.CONVERTING, details="Converting to MP3")
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
            # If conversion failed with cover art, retry without it
            if cover_path and cover_path.exists():
                logger.warning("FFmpeg failed with cover art, retrying without: %s", err[:200])
                return await self._convert_to_mp3(input_path, task, timeout=timeout, title=title, artist=artist, cover_path=None)
            raise RuntimeError(f"FFmpeg conversion failed (exit {proc.returncode}): {err}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg conversion produced no output")
        input_path.unlink(missing_ok=True)
        return output_path

    @timed("ytdlp_download")
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
            "format": "ba[ext=m4a]/ba/b",  # Falls back to combined formats when no audio-only available (datacenter IPs without PO Token)
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
                    _cover = await extract_thumbnail_from_url(thumb_url, raw_path.parent / f"_cover_{raw_path.stem}_{os.urandom(4).hex()}.jpg")
                except Exception:
                    pass
            raw_path = await self._convert_to_mp3(raw_path, task, title=_title, artist=_artist, cover_path=_cover)
            # Clean up temporary cover file
            if _cover:
                _cover.unlink(missing_ok=True)

        return raw_path, thumb_url, entry


download_manager = MusicDownloadManager()
