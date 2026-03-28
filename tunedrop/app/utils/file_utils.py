from __future__ import annotations

import asyncio
import logging
import re
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def sanitize_filename(value: str) -> str:
    value = value.replace("\x00", "").strip(". ")
    value = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", value)
    value = re.sub(r"\s+", " ", value)
    return value[:180] or "file"


def _ensure_clean_directory_sync(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def ensure_clean_directory(path: Path) -> Path:
    return await asyncio.to_thread(_ensure_clean_directory_sync, path)


def create_zip_archive(source_dir: Path, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file() and not file_path.is_symlink():
                archive.write(file_path, arcname=file_path.resolve().relative_to(source_dir.resolve()))
    return zip_path


async def cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            await asyncio.to_thread(shutil.rmtree, path, True)
        else:
            await asyncio.to_thread(path.unlink, True)


def list_audio_files(path: Path) -> list[Path]:
    return sorted([item for item in path.rglob("*.mp3") if item.is_file()])


def find_first_file(path: Path, suffix: str) -> Path | None:
    for item in sorted(path.rglob(f"*{suffix}")):
        if item.is_file():
            return item
    return None


def check_disk_space(path: Path, required_bytes: int = 500 * 1024 * 1024) -> bool:
    """Check if there's enough disk space at the given path.

    Args:
        path: The directory to check.
        required_bytes: Minimum required free bytes (default 500 MB).

    Returns:
        True if enough space is available, False otherwise.
    """
    try:
        usage = shutil.disk_usage(str(path))
        if usage.free < required_bytes:
            logger.warning(
                "Low disk space: %.0f MB free, need %.0f MB at %s",
                usage.free / (1024 * 1024), required_bytes / (1024 * 1024), path,
            )
            return False
        return True
    except OSError:
        logger.warning("Could not check disk space at %s", path, exc_info=True)
        return True  # Assume OK if we can't check
