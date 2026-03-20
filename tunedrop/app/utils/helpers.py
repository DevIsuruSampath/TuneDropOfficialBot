from __future__ import annotations

from typing import Any

from pyrogram.types import Message


async def edit_or_reply(message: Message, text: str, **kwargs: Any) -> Message:
    if message.outgoing:
        return await message.edit_text(text, **kwargs)
    return await message.reply_text(text, **kwargs)


def command_argument(message: Message) -> str:
    if not message.text:
        return ""
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""
