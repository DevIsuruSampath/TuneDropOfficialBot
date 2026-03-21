from __future__ import annotations

from typing import Any

from pyrogram.types import Message


def command_argument(message: Message) -> str:
    if not message.text:
        return ""
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""
