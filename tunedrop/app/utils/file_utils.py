from __future__ import annotations

import asyncio
import re
import shutil
import zipfile
from pathlib import Path


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:180] or "file"


def ensure_clean_directory(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_zip_archive(source_dir: Path, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, arcname=file_path.relative_to(source_dir))
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
