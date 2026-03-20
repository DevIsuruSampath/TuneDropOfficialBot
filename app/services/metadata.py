from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.utils.ffmpeg_utils import probe_audio


@dataclass(slots=True)
class AudioMetadata:
    title: str
    artist: str
    duration: int
    thumbnail_path: Path | None = None


def read_audio_metadata(file_path: Path, fallback_title: str = "Unknown Title", fallback_artist: str = "Unknown Artist") -> AudioMetadata:
    probe = probe_audio(file_path)
    format_tags = probe.get("format", {}).get("tags", {})
    title = format_tags.get("title") or fallback_title
    artist = format_tags.get("artist") or fallback_artist
    duration = int(float(probe.get("format", {}).get("duration", 0) or 0))
    return AudioMetadata(title=title, artist=artist, duration=duration)
