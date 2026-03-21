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
            await task.update("\U0001f3b5 Searching for your track...")

            spotdl_result: SubprocessResult | None = None
            try:
                spotdl_result = await self._run_spotdl(task, task.request.source, work_dir, playlist=False)
            except SubprocessFailure as exc:
                spotdl_result = exc.result
                logger.warning("spotdl failed: %s", exc)

            audio_file = find_first_file(work_dir, suffix=".mp3")

            if not audio_file and spotdl_result:
                yt_url = self._extract_youtube_url(spotdl_result)
                if not yt_url and task.request.input_type == InputType.SEARCH:
                    yt_url = f"ytsearch1:{task.request.source}"
                if yt_url:
                    await task.update("\U0001f504 Trying alternative source...")
                    try:
                        await self._run_ytdlp_download(task, yt_url, work_dir)
                    except Exception:
                        logger.exception("yt-dlp fallback also failed")
                    audio_file = find_first_file(work_dir, suffix=".mp3")

            if not audio_file:
                raise RuntimeError("Could not download the track. Please try again later.")

            metadata = await read_audio_metadata(audio_file, fallback_title=audio_file.stem)
            await task.update("\U0001f4e4 Uploading track...")
            await self._send_audio(app, message, audio_file, metadata)
            await task.update("\u2705 Completed.")
        finally:
            await cleanup_paths([work_dir])

    async def _download_spotify_playlist(self, app: Client, message: Message, task: DownloadTask) -> None:
        safe_name = sanitize_filename(f"spotify_playlist_{task.user_id}_{int(time.time())}")
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / safe_name)
        zip_path = settings.zip_dir / f"{safe_name}.zip"
        try:
            await task.update("\U0001f3b6 Downloading playlist...")
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
            await task.update("\U0001f3b5 Downloading audio...")
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
            await task.update("\U0001f4e4 Uploading track...")
            await self._send_audio(app, message, audio_file, metadata)
            await task.update("\u2705 Completed.")
        finally:
            await cleanup_paths([work_dir])

    async def _download_youtube_playlist(self, app: Client, message: Message, task: DownloadTask, info: dict[str, Any]) -> None:
        title = sanitize_filename(str(info.get("title") or f"youtube_playlist_{task.user_id}"))
        playlist_dir = await ensure_clean_directory(settings.playlists_dir / f"{title}_{int(time.time())}")
        zip_path = settings.zip_dir / f"{playlist_dir.name}.zip"
        try:
            await task.update("\U0001f3b6 Downloading playlist...")
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
                    is_error = self._is_subprocess_error_line(name, text)
                    if is_error:
                        error_lines.append(text)
                    logger.info("%s: %s", name, text)
                    if not is_error:
                        progress_text = self._map_subprocess_progress(name, text)
                        if progress_text:
                            await task.update(progress_text[:4000])
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
                return f"\U0001f3b5 {text}"
            return None

        if "processing query" in lowered:
            return "\U0001f50d Searching for your track..."
        if "found" in lowered and ("youtube" in lowered or "youtube music" in lowered):
            return "\U0001f3a7 Track found!"
        if "downloading" in lowered:
            return f"\U0001f3b5 {text}"
        if "converting" in lowered:
            return "\U0001f4a7 Converting to MP3 (320kbps)..."
        if "skipping" in lowered:
            return f"\u23ed\ufe0f {text}"
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


    async def _run_ytdlp_download(self, task: DownloadTask, url: str, out_dir: Path) -> Path:
        loop = asyncio.get_running_loop()

        def progress_hook(payload: dict[str, Any]) -> None:
            status = payload.get("status")
            if status == "downloading":
                total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
                downloaded = payload.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0
                text = f"\U0001f3b5 Downloading: {percent:.1f}%"
                asyncio.run_coroutine_threadsafe(task.update(text), loop)
            elif status == "finished":
                asyncio.run_coroutine_threadsafe(task.update("Converting to MP3 (320kbps)..."), loop)

        output_template = str(out_dir / "%(title)s.%(ext)s")
        ydl_opts = {
            **_base_ytdlp_opts(),
            "format": "bestaudio",
            "extractaudio": True,
            "outtmpl": output_template,
            "noplaylist": True,
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
        }

        def _download() -> Path:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            file_path = find_first_file(out_dir, suffix=".mp3")
            if not file_path:
                raise RuntimeError("Download failed.")
            return file_path

        return await asyncio.wait_for(asyncio.to_thread(_download), timeout=600)

    async def _run_ytdlp_playlist(self, task: DownloadTask, url: str, out_dir: Path) -> None:
        loop = asyncio.get_running_loop()

        def progress_hook(payload: dict[str, Any]) -> None:
            status = payload.get("status")
            if status == "downloading":
                filename = Path(str(payload.get("filename") or "")).stem
                total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
                downloaded = payload.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0
                idx = payload.get("playlist_index", "?")
                n_entries = payload.get("playlist_count", "?")
                asyncio.run_coroutine_threadsafe(
                    task.update(f"Downloading track {idx}/{n_entries}: {filename} ({percent:.0f}%)"),
                    loop,
                )
            elif status == "processing":
                filename = Path(str(payload.get("filename") or "")).stem
                asyncio.run_coroutine_threadsafe(
                    task.update(f"Converting to MP3 (320kbps): {filename}"),
                    loop,
                )

        ydl_opts = {
            **_base_ytdlp_opts(),
            "format": "bestaudio",
            "extractaudio": True,
            "outtmpl": str(out_dir / "%(playlist_index)s - %(title)s.%(ext)s"),
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
        }

        def _download() -> None:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await asyncio.wait_for(asyncio.to_thread(_download), timeout=1800)


download_manager = MusicDownloadManager()
