from __future__ import annotations

from pathlib import Path

import ffmpeg
import httpx

MAX_THUMBNAIL_SIZE = 5 * 1024 * 1024  # 5 MB


def probe_audio(file_path: Path) -> dict:
    return ffmpeg.probe(str(file_path))


async def extract_thumbnail_from_url(url: str, out_path: Path) -> Path | None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            size = 0
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > MAX_THUMBNAIL_SIZE:
                    return None
                chunks.append(chunk)
            out_path.write_bytes(b"".join(chunks))
            return out_path
