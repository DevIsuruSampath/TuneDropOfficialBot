from __future__ import annotations

import asyncio
import contextlib
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
from pyrogram.types import Message
from yt_dlp import YoutubeDL

from tunedrop.app.core.config import settings
from tunedrop.app.services.link_generator import link_store
from tunedrop.app.services.metadata import read_audio_metadata
from tunedrop.app.services.progress import DownloadTask
from tunedrop.app.services.uploader import upload_zip_to_storage
from tunedrop.app.services.youtube_service import extract_info
from tunedrop.app.services.zip_service import build_zip
from tunedrop.app.utils.ffmpeg_utils import extract_thumbnail_from_url
from tunedrop.app.utils.file_utils import (
    cleanup_paths,
    ensure_clean_directory,
    find_first_file,
    list_audio_files,
    sanitize_filename,
)
from tunedrop.app.utils.time_utils import estimate_download_time, format_bytes, format_seconds
from tunedrop.app.utils.validators import InputType


logger = logging.getLogger(__name__)


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
        await task.update(f"Validating request: {request.source}")

        if request.input_type in {InputType.SPOTIFY_TRACK, InputType.SEARCH, InputType.SPOTIFY_PLAYLIST}:
            await self._handle_spotify_or_search(app, message, task)
            return

        if request.input_type in {InputType.YOUTUBE_TRACK, InputType.YOUTUBE_PLAYLIST, InputType.YOUTUBE_MUSIC_TRACK, InputType.YOUTUBE_MUSIC_PLAYLIST}:
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
        work_dir = await ensure_clean_directory(settings.temp_dir / f"{task.user_id}_{int(time.time())}")
        try:
            await task.update("Downloading track with spotdl...")
            try:
                result = await self._run_spotdl(task, task.request.source, work_dir, playlist=False)
            except SubprocessFailure as exc:
                retry_failure = await self._retry_spotdl_without_ytmusic(task, task.request.source, work_dir, exc)
                if retry_failure is None:
                    result = SubprocessResult(recent_lines=tuple(), error_lines=tuple())
            else:
                retry_failure = None

            if retry_failure is not None:
                result = retry_failure.result
                fallback_url = self._extract_youtube_url(retry_failure.result.recent_lines)
                if not fallback_url:
                    raise RuntimeError(str(retry_failure)) from retry_failure
                logger.warning("spotdl failed; falling back to direct yt-dlp download: %s", fallback_url)
                await task.update("spotdl failed. Falling back to direct YouTube download...")
                await self._run_ytdlp_download(task, fallback_url, work_dir)
                result = retry_failure.result
            audio_file = find_first_file(work_dir, suffix=".mp3")
            if not audio_file:
                detail = result.last_error or result.last_line
                if detail:
                    raise RuntimeError(f"spotdl did not produce an MP3 file. Last output: {detail}")
                raise RuntimeError("spotdl did not produce an MP3 file.")

            metadata = await read_audio_metadata(audio_file, fallback_title=audio_file.stem)
            await task.update("Uploading track to Telegram...")
            await self._send_audio(app, message, audio_file, metadata)
            await task.update("Completed.")
        finally:
            await cleanup_paths([work_dir])

    async def _download_spotify_playlist(self, app: Client, message: Message, task: DownloadTask) -> None:
        safe_name = sanitize_filename(f"spotify_playlist_{task.user_id}_{int(time.time())}")
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / safe_name)
        zip_path = settings.zip_dir / f"{safe_name}.zip"
        try:
            await task.update("Downloading playlist with spotdl...")
            result = await self._run_spotdl(task, task.request.source, playlist_dir, playlist=True)
            tracks = list_audio_files(playlist_dir)
            if not tracks:
                detail = result.last_error or result.last_line
                if detail:
                    raise RuntimeError(f"Playlist download finished without MP3 files. Last output: {detail}")
                raise RuntimeError("Playlist download finished without MP3 files.")

            await task.update(f"Creating ZIP archive for {len(tracks)} tracks...")
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
                "\n".join(
                    [
                        "Playlist completed.",
                        f"Tracks: {len(tracks)}",
                        f"ZIP size: {format_bytes(upload.file_size)}",
                        f"Estimated time at {settings.download_speed_kbps:.0f} KB/s: {format_seconds(eta_seconds)}",
                        f"Download page: {link}",
                    ]
                )
            )
        finally:
            await cleanup_paths([playlist_dir, zip_path])

    async def _handle_youtube(self, app: Client, message: Message, task: DownloadTask) -> None:
        info = await extract_info(task.request.source)
        entries = info.get("entries") or []
        if entries and task.request.input_type in {InputType.YOUTUBE_PLAYLIST, InputType.YOUTUBE_MUSIC_PLAYLIST}:
            await self._download_youtube_playlist(app, message, task, info)
        else:
            await self._download_youtube_track(app, message, task, info)

    async def _download_youtube_track(self, app: Client, message: Message, task: DownloadTask, info: dict[str, Any]) -> None:
        work_dir = await ensure_clean_directory(settings.temp_dir / f"yt_{task.user_id}_{int(time.time())}")
        thumb_path: Path | None = None
        try:
            await task.update("Downloading audio from YouTube...")
            audio_file = await self._run_ytdlp_download(task, task.request.source, work_dir)
            thumb_url = info.get("thumbnail")
            if thumb_url:
                thumb_path = await extract_thumbnail_from_url(thumb_url, work_dir / "thumb.jpg")
            metadata = await read_audio_metadata(
                audio_file,
                fallback_title=str(info.get("title") or audio_file.stem),
                fallback_artist=str(info.get("uploader") or info.get("channel") or "Unknown Artist"),
            )
            metadata.thumbnail_path = thumb_path
            await task.update("Uploading track to Telegram...")
            await self._send_audio(app, message, audio_file, metadata)
            await task.update("Completed.")
        finally:
            await cleanup_paths([work_dir])

    async def _download_youtube_playlist(self, app: Client, message: Message, task: DownloadTask, info: dict[str, Any]) -> None:
        title = sanitize_filename(str(info.get("title") or f"youtube_playlist_{task.user_id}"))
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / f"{title}_{int(time.time())}")
        zip_path = settings.zip_dir / f"{playlist_dir.name}.zip"
        try:
            await task.update("Downloading YouTube playlist...")
            await self._run_ytdlp_playlist(task, task.request.source, playlist_dir)
            tracks = list_audio_files(playlist_dir)
            if not tracks:
                raise RuntimeError("Playlist download finished but no MP3 files were found.")

            await task.update(f"Creating ZIP archive for {len(tracks)} tracks...")
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
                "\n".join(
                    [
                        "Playlist completed.",
                        f"Tracks: {len(tracks)}",
                        f"ZIP size: {format_bytes(upload.file_size)}",
                        f"Estimated time at {settings.download_speed_kbps:.0f} KB/s: {format_seconds(eta_seconds)}",
                        f"Download page: {link}",
                    ]
                )
            )
        finally:
            await cleanup_paths([playlist_dir, zip_path])

    async def _send_audio(self, app: Client, message: Message, audio_file: Path, metadata: Any) -> None:
        await app.send_audio(
            chat_id=message.chat.id,
            audio=str(audio_file),
            title=metadata.title,
            performer=metadata.artist,
            duration=metadata.duration,
            thumb=str(metadata.thumbnail_path) if metadata.thumbnail_path and metadata.thumbnail_path.exists() else None,
        )

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

    async def _retry_spotdl_without_ytmusic(
        self,
        task: DownloadTask,
        source: str,
        out_dir: Path,
        failure: SubprocessFailure,
    ) -> SubprocessFailure | None:
        if not self._is_ytmusic_connectivity_failure(failure.result):
            return failure

        logger.warning("spotdl failed during YouTube Music resolution; retrying with plain YouTube only")
        await task.update("YouTube Music lookup failed. Retrying with YouTube only...")
        try:
            await self._run_spotdl(task, source, out_dir, playlist=False, audio_providers=("youtube",))
        except SubprocessFailure as retry_failure:
            return retry_failure
        return None

    async def _run_subprocess(self, task: DownloadTask, cmd: list[str], name: str) -> SubprocessResult:
        logger.info("Running %s command: %s", name, shlex.join(cmd))
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        recent_lines: deque[str] = deque(maxlen=5)
        error_lines: deque[str] = deque(maxlen=5)
        try:
            while True:
                if task.cancelled():
                    process.terminate()
                    raise asyncio.CancelledError
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=settings.spotdl_inactivity_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    process.terminate()
                    last_detail = recent_lines[-1] if recent_lines else "No output was produced."
                    raise SubprocessFailure(
                        f"{name} stalled after {int(settings.spotdl_inactivity_timeout_seconds)} seconds. "
                        f"Last output: {last_detail}",
                        SubprocessResult(recent_lines=tuple(recent_lines), error_lines=tuple(error_lines)),
                    ) from exc
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    recent_lines.append(text)
                    if self._is_subprocess_error_line(name, text):
                        error_lines.append(text)
                    logger.info("%s: %s", name, text)
                    progress_text = self._map_subprocess_progress(name, text)
                    if progress_text:
                        await task.update(progress_text[:4000])
            code = await process.wait()
            if code != 0:
                last_detail = recent_lines[-1] if recent_lines else "No error details captured."
                raise SubprocessFailure(
                    f"{name} exited with code {code}. Last output: {last_detail}",
                    SubprocessResult(recent_lines=tuple(recent_lines), error_lines=tuple(error_lines)),
                )
            if error_lines:
                raise SubprocessFailure(
                    f"{name} failed. Last output: {error_lines[-1]}",
                    SubprocessResult(recent_lines=tuple(recent_lines), error_lines=tuple(error_lines)),
                )
            return SubprocessResult(recent_lines=tuple(recent_lines), error_lines=tuple(error_lines))
        finally:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    def _map_subprocess_progress(self, name: str, text: str) -> str | None:
        lowered = text.lower()
        if name != "spotdl":
            if "downloading" in lowered or "converting" in lowered or "processing" in lowered:
                return text
            return None

        if "processing query" in lowered:
            return "Resolving Spotify track..."
        if "found" in lowered and ("youtube" in lowered or "youtube music" in lowered):
            return "Matched Spotify track to an audio source..."
        if "downloading" in lowered:
            return text
        if "converting" in lowered:
            return "Converting audio to MP3..."
        if "skipping" in lowered:
            return text
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

    def _extract_youtube_url(self, lines: tuple[str, ...]) -> str | None:
        for text in reversed(lines):
            match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)\S+", text)
            if match:
                url = match.group(0).rstrip(".,;:!)\"]'")
                return url
        return None

    def _is_ytmusic_connectivity_failure(self, result: SubprocessResult) -> bool:
        joined = "\n".join((*result.recent_lines, *result.error_lines)).lower()
        ytmusic_markers = (
            "music.youtube.com",
            "ytmusic",
            "youtube music",
            "check_ytmusic_connection",
            "to resolve 'music.youtube.com'",
        )
        connectivity_markers = (
            "temporary failure in name resolution",
            "nameresolutionerror",
            "maxretryerror",
            "connectionerror",
            "failed to establish a new connection",
        )
        return any(marker in joined for marker in ytmusic_markers) and any(
            marker in joined for marker in connectivity_markers
        )

    async def _run_ytdlp_download(self, task: DownloadTask, url: str, out_dir: Path) -> Path:
        loop = asyncio.get_running_loop()

        def progress_hook(payload: dict[str, Any]) -> None:
            status = payload.get("status")
            if status == "downloading":
                total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
                downloaded = payload.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0
                text = f"Downloading from YouTube: {percent:.1f}%"
                asyncio.run_coroutine_threadsafe(task.update(text), loop)
            elif status == "finished":
                asyncio.run_coroutine_threadsafe(task.update("Converting downloaded audio to MP3..."), loop)

        output_template = str(out_dir / "%(title)s.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "windowsfilenames": True,
            "restrictfilenames": True,
            "progress_hooks": [progress_hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
            "js_runtimes": {"node": {}},
        }

        def _download() -> Path:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            file_path = find_first_file(out_dir, suffix=".mp3")
            if not file_path:
                raise RuntimeError("yt-dlp did not produce an MP3 file.")
            return file_path

        return await asyncio.wait_for(asyncio.to_thread(_download), timeout=600)

    async def _run_ytdlp_playlist(self, task: DownloadTask, url: str, out_dir: Path) -> None:
        loop = asyncio.get_running_loop()

        def progress_hook(payload: dict[str, Any]) -> None:
            if payload.get("status") == "downloading":
                filename = Path(str(payload.get("filename") or "")).name
                asyncio.run_coroutine_threadsafe(
                    task.update(f"Downloading playlist item: {filename or 'current track'}"),
                    loop,
                )

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / "%(playlist_index)s - %(title)s.%(ext)s"),
            "quiet": True,
            "windowsfilenames": True,
            "restrictfilenames": True,
            "progress_hooks": [progress_hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
            "playlistend": settings.max_playlist_items,
            "js_runtimes": {"node": {}},
        }

        def _download() -> None:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await asyncio.wait_for(asyncio.to_thread(_download), timeout=1800)


download_manager = MusicDownloadManager()
