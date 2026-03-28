from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from pyrogram.enums import ParseMode
from pyrogram.types import Message

from tunedrop.app.core.config import settings

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

_seen_keys: OrderedDict[tuple[int, int], None] = OrderedDict()
_seen_max = 500

# Per-user rate limiting: {user_id: [timestamps]}
_rate_limit_store: dict[int, list[float]] = {}
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX_REQUESTS = 10  # requests per window


def _msg_key(message: Message) -> tuple[int, int]:
    """Return a unique key for deduplication."""
    return (message.chat.id, message.id)


def once_per_message(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapper(_, message: Message, *args: Any, **kwargs: Any) -> Any:
        key = _msg_key(message)
        if key in _seen_keys:
            logger.debug("once_per_message: deduped message %d in chat %d", message.id, message.chat.id)
            return None
        _seen_keys[key] = None
        _seen_keys.move_to_end(key)
        while len(_seen_keys) > _seen_max:
            _seen_keys.popitem(last=False)
        return await handler(_, message, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def rate_limit(handler: Handler) -> Handler:
    """Limit per-user request rate. Shows cooldown message if exceeded."""
    @wraps(handler)
    async def wrapper(_, message: Message, *args: Any, **kwargs: Any) -> Any:
        user_id = message.from_user.id if message.from_user else 0
        if not user_id:
            return await handler(_, message, *args, **kwargs)

        now = time.monotonic()
        timestamps = _rate_limit_store.get(user_id, [])
        # Prune expired entries
        timestamps = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
        timestamps.append(now)
        _rate_limit_store[user_id] = timestamps

        if len(timestamps) > _RATE_LIMIT_MAX_REQUESTS:
            oldest = timestamps[0]
            cooldown = int(_RATE_LIMIT_WINDOW - (now - oldest))
            await message.reply_text(
                f"⏳ Too many requests. Wait <b>{cooldown}s</b> and try again.",
                parse_mode=ParseMode.HTML,
            )
            return None
        return await handler(_, message, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def admin_only(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapper(_, update, *args: Any, **kwargs: Any) -> Any:
        user = update.from_user
        if not user or user.id not in settings.admin_user_ids:
            if hasattr(update, "reply_text"):
                await update.reply_text("You are not allowed to use this command.")
            else:
                try:
                    await update.answer("Not allowed.", show_alert=True)
                except Exception:
                    pass
            return None
        return await handler(_, update, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
