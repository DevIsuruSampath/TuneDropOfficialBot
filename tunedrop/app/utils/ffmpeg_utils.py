from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import ffmpeg
import httpx

MAX_THUMBNAIL_SIZE = 5 * 1024 * 1024  # 5 MB
logger = logging.getLogger(__name__)

_shared_client: httpx.AsyncClient | None = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
        )
    return _shared_client


async def close_shared_client() -> None:
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


def probe_audio(file_path: Path) -> dict:
    return ffmpeg.probe(str(file_path))


async def async_probe_audio(file_path: Path) -> dict:
    return await asyncio.to_thread(probe_audio, file_path)


async def extract_thumbnail_from_url(url: str, out_path: Path) -> Path | None:
    client = await _get_shared_client()
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            size = 0
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_THUMBNAIL_SIZE:
                        f.close()
                        out_path.unlink(missing_ok=True)
                        return None
                    f.write(chunk)
            if out_path.exists() and out_path.stat().st_size > 0:
                return out_path
            return None
    except Exception:
        logger.warning("Failed to download thumbnail from %s", url, exc_info=True)
        return None


# YouTube thumbnail sizes in order of preference
_YT_THUMB_SIZES = ("maxresdefault", "sddefault", "hqdefault", "mqdefault")


async def extract_youtube_thumbnail(yt_id: str, out_path: Path) -> Path | None:
    """Try multiple YouTube thumbnail sizes until one succeeds."""
    for size in _YT_THUMB_SIZES:
        url = f"https://i.ytimg.com/vi/{yt_id}/{size}.jpg"
        result = await extract_thumbnail_from_url(url, out_path)
        if result:
            return result
    logger.warning("All YouTube thumbnail sizes failed for video %s", yt_id)
    return None


async def extract_cover_from_mp3(mp3_path: Path, out_path: Path) -> Path | None:
    """Extract embedded cover art from an MP3 file using FFmpeg."""
    if not mp3_path.exists():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(mp3_path),
            "-an", "-vcodec", "copy",
            str(out_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return out_path
        out_path.unlink(missing_ok=True)
        return None
    except Exception:
        logger.debug("Failed to extract cover art from %s", mp3_path.name, exc_info=True)
        return None
