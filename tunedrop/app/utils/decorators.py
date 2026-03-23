from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from pyrogram.types import Message

from tunedrop.app.core.config import settings

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

_seen_keys: set[int] = set()
_seen_max = 500
_evict_batch = 100


def _msg_key(message: Message) -> int:
    """Return a unique key for deduplication: chat_id * 10^9 + message_id."""
    return (message.chat.id % 1_000_000_000) * 1_000_000_000 + (message.id % 1_000_000_000)


def once_per_message(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapper(_, message: Message, *args: Any, **kwargs: Any) -> Any:
        key = _msg_key(message)
        if key in _seen_keys:
            logger.debug("once_per_message: deduped message %d in chat %d", message.id, message.chat.id)
            return None
        _seen_keys.add(key)
        if len(_seen_keys) > _seen_max:
            # Evict oldest batch instead of clearing all keys
            evict = sorted(_seen_keys)[:_evict_batch]
            for k in evict:
                _seen_keys.discard(k)
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
