from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from tunedrop.app.core.config import settings

logger = logging.getLogger(__name__)


def _base_ytdlp_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "keepvideo": False,
        "writethumbnail": False,
        "nopart": True,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10485760,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "throttledratelimit": 100000,
    }
    if settings.ytdlp_cookie_file:
        cookie_path = Path(settings.ytdlp_cookie_file)
        if cookie_path.is_file() and cookie_path.stat().st_size > 0:
            opts["cookiefile"] = settings.ytdlp_cookie_file
    # Use aria2c as external downloader if available
    try:
        import shutil
        if shutil.which("aria2c"):
            opts["downloader"] = "aria2c"
            opts["downloader_args"] = ["aria2c:-x 16 -s 16 -j 16 -k 1M"]
    except Exception:
        pass
    return opts


async def extract_info(url: str) -> dict[str, Any]:
    def _extract() -> dict[str, Any]:
        opts = {
            **_base_ytdlp_opts(),
            "noplaylist": False,
            "extract_flat": "in_playlist",
        }
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.wait_for(asyncio.to_thread(_extract), timeout=settings.spotdl_inactivity_timeout_seconds)


