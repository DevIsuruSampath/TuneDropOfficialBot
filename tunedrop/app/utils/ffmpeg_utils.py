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
        logger.debug("Failed to download thumbnail from %s", url, exc_info=True)
        return None
