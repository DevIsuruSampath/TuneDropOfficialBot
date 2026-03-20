from __future__ import annotations

from pyrogram import filters

from tunedrop.app.utils.validators import is_supported_url


music_input = filters.create(
    lambda _, __, message: bool(message.text and is_supported_url(message.text.strip()))
)
