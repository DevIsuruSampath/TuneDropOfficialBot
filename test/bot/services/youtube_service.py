from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL


@dataclass(slots=True)
class YoutubeInfo:
    title: str
    artist: str
    duration: int | None
    thumbnail: str | None
    playlist_count: int = 0


async def extract_info(url: str) -> dict[str, Any]:
    def _extract() -> dict[str, Any]:
        with YoutubeDL({"quiet": True, "noplaylist": False, "extract_flat": "in_playlist"}) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.to_thread(_extract)


def normalize_info(payload: dict[str, Any]) -> YoutubeInfo:
    uploader = str(payload.get("uploader") or payload.get("channel") or "Unknown Artist")
    return YoutubeInfo(
        title=str(payload.get("title") or "Unknown Title"),
        artist=uploader,
        duration=payload.get("duration"),
        thumbnail=payload.get("thumbnail"),
        playlist_count=len(payload.get("entries") or []),
    )
