from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import logging
import re
import shlex
import shutil
import subprocess
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
    build_completion_message,
    build_large_file_message,
    build_playlist_completion,
    build_progress_message,
    escape_html,
)
from tunedrop.app.utils.validators import InputType


logger = logging.getLogger(__name__)

_TELEGRAM_BOT_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50MB
_PROGRESS_UPDATE_INTERVAL = 4.0  # seconds between Telegram message edits
_CONVERSION_TIMEOUT_BASE = 240  # minimum seconds for FFmpeg audio conversion
_MAX_AUDIO_DURATION = 20 * 60  # 20 minutes — exceeds 50MB at 320kbps
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
        await task.update(f"<b>🔍 Looking up</b> <code>{escape_html(request.source)}</code>", parse_mode=ParseMode.HTML)

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
                    await self._send_cached_audio(app, message, cached)
                    await task.update(build_completion_message(), parse_mode=ParseMode.HTML)
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
            thumb_url: str | None = None
            yt_info: dict[str, Any] | None = None

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
            cache_key, cache_key_type = generate_cache_key(task.request.source, task.request.input_type, yt_info)
            if cache_key:
                try:
                    await task.update(build_progress_message(DownloadPhase.UPLOADING), parse_mode=ParseMode.HTML)
                    audio_file_id, thumb_file_id = await song_cache.upload_to_cache_channel(
                        app, audio_file, metadata.title, metadata.artist, metadata.duration, metadata.thumbnail_path,
                    )
                    await song_cache.cache_song(
                        cache_key=cache_key,
                        key_type=cache_key_type,
                        file_id=audio_file_id,
                        title=metadata.title,
                        artist=metadata.artist,
                        duration=metadata.duration,
                        file_size=file_size,
                        thumbnail_file_id=thumb_file_id,
                    )
                except Exception:
                    logger.exception("Failed to cache song, sending directly to user")

            await self._deliver_audio(app, message, audio_file, metadata, task)
            await task.update(build_completion_message(), parse_mode=ParseMode.HTML)
        finally:
            await cleanup_paths([work_dir])

    async def _download_spotify_playlist(self, app: Client, message: Message, task: DownloadTask) -> None:
        safe_name = sanitize_filename(f"spotify_playlist_{task.user_id}_{int(time.time())}")
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / safe_name)
        zip_path = settings.zip_dir / f"{safe_name}.zip"
        try:
            await task.update(build_progress_message(DownloadPhase.DOWNLOADING, details="Downloading playlist..."), parse_mode=ParseMode.HTML)
            result = await self._run_spotdl(task, task.request.source, playlist_dir, playlist=True)
            tracks = list_audio_files(playlist_dir)
            if not tracks:
                detail = result.last_error or result.last_line
                if detail:
                    raise RuntimeError(f"Playlist download finished without MP3 files. Last output: {detail}")
                raise RuntimeError("Playlist download finished without MP3 files.")

            await task.update(build_progress_message(DownloadPhase.CONVERTING, details=f"Creating ZIP archive for {len(tracks)} tracks..."), parse_mode=ParseMode.HTML)
            await build_zip(playlist_dir, zip_path)
            upload = await upload_zip_to_storage(app, zip_path, caption=f"Playlist archive for user {task.user_id}")
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
            await task.update(
                build_playlist_completion(
                    track_count=len(tracks),
                    file_size=upload.file_size,
                    download_link=link,
                    estimated_time=eta_seconds,
                    speed_kbps=settings.download_speed_kbps,
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
                    await self._send_cached_audio(app, message, cached)
                    await task.update(build_completion_message(), parse_mode=ParseMode.HTML)
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
            if cache_key:
                try:
                    await task.update(build_progress_message(DownloadPhase.UPLOADING), parse_mode=ParseMode.HTML)
                    audio_file_id, thumb_file_id = await song_cache.upload_to_cache_channel(
                        app, audio_file, metadata.title, metadata.artist, metadata.duration, thumb_path,
                    )
                    await song_cache.cache_song(
                        cache_key=cache_key,
                        key_type=cache_key_type,
                        file_id=audio_file_id,
                        title=metadata.title,
                        artist=metadata.artist,
                        duration=metadata.duration,
                        file_size=file_size,
                        thumbnail_file_id=thumb_file_id,
                    )
                except Exception:
                    logger.exception("Failed to cache song, sending directly to user")

            await self._deliver_audio(app, message, audio_file, metadata, task)
            await task.update(build_completion_message(), parse_mode=ParseMode.HTML)
        finally:
            await cleanup_paths([work_dir])

    async def _download_youtube_playlist(self, app: Client, message: Message, task: DownloadTask, info: dict[str, Any]) -> None:
        title = sanitize_filename(str(info.get("title") or f"youtube_playlist_{task.user_id}"))
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / f"{title}_{int(time.time())}")
        zip_path = settings.zip_dir / f"{playlist_dir.name}.zip"
        try:
            await task.update(build_progress_message(DownloadPhase.DOWNLOADING, details="Downloading playlist..."), parse_mode=ParseMode.HTML)
            await self._run_ytdlp_playlist(task, task.request.source, playlist_dir)
            tracks = list_audio_files(playlist_dir)
            if not tracks:
                raise RuntimeError("Playlist download finished but no MP3 files were found.")

            await task.update(build_progress_message(DownloadPhase.CONVERTING, details=f"Creating ZIP archive for {len(tracks)} tracks..."), parse_mode=ParseMode.HTML)
            await build_zip(playlist_dir, zip_path)
            upload = await upload_zip_to_storage(app, zip_path, caption=f"YouTube playlist archive for user {task.user_id}")
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
            await task.update(
                build_playlist_completion(
                    track_count=len(tracks),
                    file_size=upload.file_size,
                    download_link=link,
                    estimated_time=eta_seconds,
                    speed_kbps=settings.download_speed_kbps,
                ),
                parse_mode=ParseMode.HTML,
            )
        finally:
            await cleanup_paths([playlist_dir, zip_path])

    async def _resolve_search_and_check_cache(self, app: Client, message: Message, task: DownloadTask) -> str | None:
        """Resolve a search query to a YouTube URL, checking cache.

        Returns the YouTube URL if not cached (caller should download).
        Returns None if cache hit (song already sent to user).
        Raises RuntimeError if resolution fails entirely.
        """
        sentinel = object()
        try:
            search_info = await extract_info(f"ytsearch1:{task.request.source}")
            if search_info is None:
                return sentinel  # type: ignore[return-value]
            entries = search_info.get("entries") or []
            if not entries:
                return sentinel  # type: ignore[return-value]
            entry = entries[0]
            yt_id = entry.get("id")
            if not yt_id:
                return sentinel  # type: ignore[return-value]

            cache_key, _ = generate_cache_key(task.request.source, task.request.input_type, entry)
            if cache_key:
                cached = await song_cache.get_cached_song(cache_key)
                if cached:
                    try:
                        await self._send_cached_audio(app, message, cached)
                        await task.update(build_completion_message(), parse_mode=ParseMode.HTML)
                        return None  # Cache hit
                    except Exception:
                        logger.warning("Cached file send failed for %s, re-downloading", cache_key)
                        await song_cache.invalidate_cache(cache_key)

            return f"https://www.youtube.com/watch?v={yt_id}"
        except Exception:
            logger.exception("Search resolution failed, falling back to ytsearch download")
            return sentinel  # type: ignore[return-value]

    async def _send_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any, reply_markup: InlineKeyboardMarkup | None = None) -> None:
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

    async def _send_cached_audio(self, app: Client, message: Message, cached: dict[str, Any]) -> None:
        """Send a cached song to the user using the stored Telegram file_id."""
        username = await self._get_bot_username(app)
        audio_markup = build_audio_keyboard(username) if username else None
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

    async def _deliver_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any, task: DownloadTask) -> None:
        """Send audio directly if under 50MB, otherwise upload to channel and send link."""
        file_size = audio_file.stat().st_size
        if file_size <= _TELEGRAM_BOT_UPLOAD_LIMIT:
            username = await self._get_bot_username(app)
            audio_markup = build_audio_keyboard(username) if username else None
            await self._send_audio(app, message, audio_file, metadata, reply_markup=audio_markup)
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
            cmd.extend(["--cookie-file", settings.spotify_cookie_file])
        return await self._run_subprocess(task, cmd, "spotdl")

    _FIRST_OUTPUT_TIMEOUT: int = 60
    _STALL_TIMEOUT: int = 90

    async def _run_subprocess(self, task: DownloadTask, cmd: list[str], name: str) -> SubprocessResult:
        logger.info("Running %s command: %s", name, shlex.join(cmd))
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        recent_lines: deque[str] = deque(maxlen=50)
        error_lines: deque[str] = deque(maxlen=20)
        has_output = False
        try:
            while True:
                if task.cancelled():
                    process.terminate()
                    raise asyncio.CancelledError
                timeout = settings.spotdl_inactivity_timeout_seconds if has_output else self._FIRST_OUTPUT_TIMEOUT
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError as exc:
                    process.terminate()
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
                        progress_text = self._map_subprocess_progress(name, text)
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
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    def _map_subprocess_progress(self, name: str, text: str) -> str | None:
        lowered = text.lower()
        if name != "spotdl":
            if "downloading" in lowered or "converting" in lowered or "processing" in lowered:
                return build_progress_message(DownloadPhase.DOWNLOADING)
            return None

        if "processing query" in lowered:
            return build_progress_message(DownloadPhase.SEARCHING)
        if "downloading" in lowered:
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

    async def _convert_to_mp3(self, input_path: Path, task: DownloadTask, timeout: float | None = None) -> Path:
        """Convert an audio file to MP3 using FFmpeg as a subprocess with a timeout."""
        duration = await self._validate_audio_file(input_path)
        # Dynamic timeout: scale with audio duration (2x real-time + 120s overhead, minimum 240s)
        if timeout is None:
            timeout = max(int(duration) * 2 + 120, _CONVERSION_TIMEOUT_BASE)
        output_path = input_path.with_suffix(".mp3")
        text = build_progress_message(DownloadPhase.CONVERTING, details="Converting to MP3...")
        await task.update(text, parse_mode=ParseMode.HTML)
        log_path = input_path.with_suffix(".ffmpeg.log")
        log_file = open(log_path, "w")  # noqa: SIM115
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin",
                "-analyzeduration", "10M", "-probesize", "10M",
                "-i", str(input_path),
                "-vn", "-codec:a", "libmp3lame", "-b:a", "320k",
                str(output_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log_file,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"FFmpeg conversion timed out after {timeout}s")
            if proc.returncode != 0:
                log_file.close()
                detail = log_path.read_text(errors="replace")[-300:]
                raise RuntimeError(f"FFmpeg conversion failed (exit {proc.returncode}): {detail}")
        finally:
            log_file.close()
            log_path.unlink(missing_ok=True)
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
                text = build_progress_message(DownloadPhase.DOWNLOADING, percentage=percent, eta=eta)
                last_progress_time[0] = now
                asyncio.run_coroutine_threadsafe(task.update(text, parse_mode=ParseMode.HTML), loop)

        output_template = str(out_dir / "%(title)s.%(ext)s")
        ydl_opts = {
            **_base_ytdlp_opts(),
            "format": "bestaudio",
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
            raw_path = await self._convert_to_mp3(raw_path, task)

        return raw_path, thumb_url, entry

    async def _run_ytdlp_playlist(self, task: DownloadTask, url: str, out_dir: Path) -> None:
        loop = asyncio.get_running_loop()
        last_progress_time = [0.0]

        def progress_hook(payload: dict[str, Any]) -> None:
            now = time.monotonic()
            if now - last_progress_time[0] < _PROGRESS_UPDATE_INTERVAL:
                return
            status = payload.get("status")
            if status == "downloading":
                filename = Path(str(payload.get("filename") or "")).stem
                total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
                downloaded = payload.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0
                eta = payload.get("eta")
                idx = payload.get("playlist_index", "?")
                n_entries = payload.get("playlist_count", "?")
                last_progress_time[0] = now
                asyncio.run_coroutine_threadsafe(
                    task.update(
                        build_progress_message(
                            DownloadPhase.DOWNLOADING,
                            percentage=percent,
                            eta=eta,
                            details=f"Track {idx}/{n_entries}: {filename}",
                        ),
                        parse_mode=ParseMode.HTML,
                    ),
                    loop,
                )

        ydl_opts = {
            **_base_ytdlp_opts(),
            "format": "bestaudio",
            "outtmpl": str(out_dir / "%(playlist_index)s - %(title)s.%(ext)s"),
            "windowsfilenames": True,
            "restrictfilenames": True,
            "progress_hooks": [progress_hook],
            "playlistend": settings.max_playlist_items,
        }

        def _download() -> None:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await asyncio.wait_for(asyncio.to_thread(_download), timeout=1800)

        # Convert any non-MP3 files to MP3
        for raw_file in list(out_dir.iterdir()):
            if raw_file.is_file() and raw_file.suffix.lower() != ".mp3":
                await self._convert_to_mp3(raw_file, task)


download_manager = MusicDownloadManager()
