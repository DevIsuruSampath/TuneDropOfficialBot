from __future__ import annotations

import asyncio
from typing import Any

from yt_dlp import YoutubeDL

from tunedrop.app.core.config import settings


async def extract_info(url: str) -> dict[str, Any]:
    def _extract() -> dict[str, Any]:
        with YoutubeDL({"quiet": True, "noplaylist": False, "extract_flat": "in_playlist", "js_runtimes": {"node": {}}}) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.wait_for(asyncio.to_thread(_extract), timeout=settings.spotdl_inactivity_timeout_seconds)
