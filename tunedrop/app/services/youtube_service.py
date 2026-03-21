from __future__ import annotations

import asyncio
from typing import Any

from yt_dlp import YoutubeDL

from tunedrop.app.core.config import settings


def _base_ytdlp_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "js_runtimes": {"node": {}},
        "keepvideo": False,
        "writethumbnail": False,
        "nopart": True,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10485760,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if settings.ytdlp_cookie_file:
        opts["cookiefile"] = settings.ytdlp_cookie_file
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
