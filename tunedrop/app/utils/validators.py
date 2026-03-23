from __future__ import annotations

import re
from enum import StrEnum


class InputType(StrEnum):
    SPOTIFY_TRACK = "spotify_track"
    SPOTIFY_PLAYLIST = "spotify_playlist"
    YOUTUBE_MUSIC_TRACK = "youtube_music_track"
    YOUTUBE_MUSIC_PLAYLIST = "youtube_music_playlist"
    SEARCH = "search"
    UNKNOWN = "unknown"


SPOTIFY_TRACK_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[^/]+/)?track/[A-Za-z0-9]+(?:\?.*)?$"
)
SPOTIFY_PLAYLIST_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[^/]+/)?playlist/[A-Za-z0-9]+(?:\?.*)?$"
)
YOUTUBE_MUSIC_RE = re.compile(r"^https?://music\.youtube\.com/")


def classify_input(value: str) -> InputType:
    text = value.strip()
    if SPOTIFY_TRACK_RE.match(text):
        return InputType.SPOTIFY_TRACK
    if SPOTIFY_PLAYLIST_RE.match(text):
        return InputType.SPOTIFY_PLAYLIST
    if YOUTUBE_MUSIC_RE.match(text):
        return InputType.YOUTUBE_MUSIC_PLAYLIST if "list=" in text else InputType.YOUTUBE_MUSIC_TRACK
    if text:
        return InputType.SEARCH
    return InputType.UNKNOWN


_URL_RE = re.compile(r"https?://\S+")


def is_supported_url(value: str) -> bool:
    input_type = classify_input(value)
    return input_type not in {InputType.UNKNOWN, InputType.SEARCH}


def looks_like_url(value: str) -> bool:
    return bool(_URL_RE.search(value.strip()))
