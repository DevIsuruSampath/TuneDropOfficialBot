from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from pyrogram.types import Message

from tunedrop.app.core.config import settings

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

_seen_keys: OrderedDict[int, None] = OrderedDict()
_seen_max = 500


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
        _seen_keys[key] = None
        _seen_keys.move_to_end(key)
        # Evict oldest entries when over capacity
        while len(_seen_keys) > _seen_max:
            _seen_keys.popitem(last=False)
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
