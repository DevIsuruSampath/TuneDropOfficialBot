from __future__ import annotations

from pyrogram import filters

from tunedrop.app.utils.validators import is_supported_url


_seen: set[tuple[int, int]] = set()


def _is_fresh(_, __, message) -> bool:
    key = (message.chat.id, message.id)
    if key in _seen:
        return False
    _seen.add(key)
    if len(_seen) > 200:
        _seen.clear()
    return True


fresh = filters.create(_is_fresh)

music_input = filters.create(
    lambda _, __, message: bool(message.text and is_supported_url(message.text.strip()))
)
