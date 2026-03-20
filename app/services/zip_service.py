from __future__ import annotations

import asyncio
from pathlib import Path

from app.utils.file_utils import create_zip_archive


async def build_zip(source_dir: Path, zip_path: Path) -> Path:
    return await asyncio.to_thread(create_zip_archive, source_dir, zip_path)
