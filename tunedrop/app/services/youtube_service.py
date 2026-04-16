from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from tunedrop.app.core.config import settings
from tunedrop.app.utils.search_utils import build_search_candidates, first_valid_entry

logger = logging.getLogger(__name__)

# Limit concurrent yt-dlp extractions to prevent thread pool saturation
_EXTRACT_SEMAPHORE = asyncio.Semaphore(20)


def _base_ytdlp_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "keepvideo": False,
        "writethumbnail": False,
        "nopart": True,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10485760,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "throttledratelimit": 100000,
    }
    # Do not force YouTube player clients here. Recent yt-dlp/YouTube changes
    # require PO tokens for Android/Web download formats in some environments,
    # which causes resolved /song searches to fail with no output file.
    # Let yt-dlp choose its current safe defaults instead.
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
    async with _EXTRACT_SEMAPHORE:
        def _extract() -> dict[str, Any]:
            opts = {
                **_base_ytdlp_opts(),
                "noplaylist": False,
                "extract_flat": "in_playlist",
            }
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        return await asyncio.wait_for(asyncio.to_thread(_extract), timeout=settings.spotdl_inactivity_timeout_seconds)


async def search_youtube_music(query: str) -> dict[str, Any] | None:
    """Search YouTube Music and return the first result's info.

    Uses YouTube Music search URL which returns music-specific results.
    Falls back to regular YouTube search and query variants if needed.
    """
    import urllib.parse

    def _search_ytmusic(search_query: str) -> dict[str, Any] | None:
        opts = {
            **_base_ytdlp_opts(),
            "extract_flat": True,
        }
        encoded_q = urllib.parse.quote(search_query)
        ytm_url = f"https://music.youtube.com/search?q={encoded_q}"
        with YoutubeDL(opts) as ydl:
            return first_valid_entry(ydl.extract_info(ytm_url, download=False))

    def _search_ytsearch(search_query: str) -> dict[str, Any] | None:
        opts = {
            **_base_ytdlp_opts(),
            "extract_flat": "in_playlist",
            "noplaylist": False,
        }
        with YoutubeDL(opts) as ydl:
            return first_valid_entry(ydl.extract_info(f"ytsearch1:{search_query}", download=False))

    search_queries = build_search_candidates(query)
    async with _EXTRACT_SEMAPHORE:
        for candidate in search_queries:
            try:
                entry = await asyncio.wait_for(
                    asyncio.to_thread(_search_ytmusic, candidate),
                    timeout=settings.spotdl_inactivity_timeout_seconds,
                )
                if entry:
                    logger.info("Resolved search %r via YouTube Music query %r", query, candidate)
                    return entry
            except Exception:
                logger.warning("YouTube Music search failed for %r", candidate, exc_info=True)

        for candidate in search_queries:
            try:
                entry = await asyncio.wait_for(
                    asyncio.to_thread(_search_ytsearch, candidate),
                    timeout=settings.spotdl_inactivity_timeout_seconds,
                )
                if entry:
                    logger.info("Resolved search %r via ytsearch query %r", query, candidate)
                    return entry
            except Exception:
                logger.warning("ytsearch fallback failed for %r", candidate, exc_info=True)

    return None
