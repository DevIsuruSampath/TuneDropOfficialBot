from __future__ import annotations

import re
from typing import Any


_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})")


def _normalize_query(value: str) -> str:
    return " ".join(value.strip().split())


def _add_candidate(candidates: list[str], seen: set[str], value: str) -> None:
    cleaned = _normalize_query(value)
    if not cleaned:
        return
    key = cleaned.casefold()
    if key in seen:
        return
    seen.add(key)
    candidates.append(cleaned)


def _looks_like_artist_fragment(value: str) -> bool:
    letters = [char for char in value if char.isalpha()]
    if len(letters) < 4:
        return False
    uppercase_ratio = sum(char.isupper() for char in letters) / len(letters)
    return uppercase_ratio >= 0.7 or any(char.isdigit() for char in value) or value.startswith("@")


def build_search_candidates(query: str) -> list[str]:
    """Build progressively looser search variants for free-text song lookups."""
    cleaned = _normalize_query(query)
    if not cleaned:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    _add_candidate(candidates, seen, cleaned)

    if " - " in cleaned:
        left, right = (part.strip() for part in cleaned.split(" - ", 1))
        if left and right:
            _add_candidate(candidates, seen, f"{left} {right}")
            _add_candidate(candidates, seen, f"{right} - {left}")
            _add_candidate(candidates, seen, f"{right} {left}")
    else:
        words = cleaned.split()
        if len(words) == 2:
            left, right = words
            _add_candidate(candidates, seen, f"{right} - {left}")

        if len(words) >= 2:
            first = words[0]
            rest = " ".join(words[1:])
            last = words[-1]
            leading = " ".join(words[:-1])
            if _looks_like_artist_fragment(first):
                _add_candidate(candidates, seen, f"{first} - {rest}")
            if _looks_like_artist_fragment(last):
                _add_candidate(candidates, seen, f"{last} - {leading}")

    _add_candidate(candidates, seen, f"{cleaned} audio")
    _add_candidate(candidates, seen, f"{cleaned} official audio")

    return candidates


def first_valid_entry(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the first non-null yt-dlp entry from a search/extract result."""
    if not isinstance(result, dict):
        return None

    entries = result.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                return entry
        return None

    return result


def extract_youtube_id(entry: dict[str, Any] | None) -> str | None:
    """Extract a YouTube video ID from an entry dict or one of its URLs."""
    if not isinstance(entry, dict):
        return None

    video_id = entry.get("id")
    if isinstance(video_id, str) and video_id:
        return video_id

    for key in ("url", "webpage_url", "original_url"):
        value = entry.get(key)
        if not isinstance(value, str):
            continue
        match = _YOUTUBE_ID_RE.search(value)
        if match:
            return match.group(1)

    return None
