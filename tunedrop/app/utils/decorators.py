from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from pyrogram.types import Message

from tunedrop.app.core.config import settings


Handler = Callable[..., Awaitable[Any]]

_seen_ids: set[int] = set()


def once_per_message(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapper(_, message: Message, *args: Any, **kwargs: Any) -> Any:
        mid = message.id
        if mid in _seen_ids:
            return None
        _seen_ids.add(mid)
        if len(_seen_ids) > 500:
            _seen_ids.clear()
        return await handler(_, message, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def admin_only(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapper(_, message: Message, *args: Any, **kwargs: Any) -> Any:
        user = message.from_user
        if not user or user.id not in settings.admin_user_ids:
            await message.reply_text("You are not allowed to use this command.")
            return None
        return await handler(_, message, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
