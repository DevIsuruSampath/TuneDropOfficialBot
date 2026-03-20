from __future__ import annotations

import asyncio
from pathlib import Path

import ffmpeg
import httpx


def probe_audio(file_path: Path) -> dict:
    return ffmpeg.probe(str(file_path))


async def extract_thumbnail_from_url(url: str, out_path: Path) -> Path | None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        response = await client.get(url)
        response.raise_for_status()
        out_path.write_bytes(response.content)
        return out_path
