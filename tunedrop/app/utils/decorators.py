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
_rate_limit_last_prune: float = 0.0
_RATE_LIMIT_PRUNE_INTERVAL = 300  # prune stale entries every 5 minutes

# Cached channel invite link (resolved once)
_channel_link_cache: str | None = None

# Membership cache: {user_id: (is_member, timestamp)}
_membership_cache: dict[int, tuple[bool, float]] = {}
_MEMBERSHIP_CACHE_TTL = 300  # 5 minutes


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


def _prune_rate_limit_store() -> None:
    """Remove stale user entries from the rate limit store."""
    global _rate_limit_last_prune
    now = time.monotonic()
    if now - _rate_limit_last_prune < _RATE_LIMIT_PRUNE_INTERVAL:
        return
    _rate_limit_last_prune = now
    stale_users = [
        uid for uid, timestamps in _rate_limit_store.items()
        if not timestamps or now - timestamps[-1] > _RATE_LIMIT_WINDOW
    ]
    for uid in stale_users:
        del _rate_limit_store[uid]


def rate_limit(handler: Handler) -> Handler:
    """Limit per-user request rate. Shows cooldown message if exceeded."""
    @wraps(handler)
    async def wrapper(_, message: Message, *args: Any, **kwargs: Any) -> Any:
        user_id = message.from_user.id if message.from_user else 0
        if not user_id:
            return await handler(_, message, *args, **kwargs)

        now = time.monotonic()
        _prune_rate_limit_store()
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


async def _get_channel_link(client: Any) -> str:
    """Resolve and cache the force-sub channel's invite link."""
    global _channel_link_cache
    if _channel_link_cache:
        return _channel_link_cache

    channel_id = settings.force_sub_channel_id
    try:
        chat = await client.get_chat(channel_id)
        # Use invite_link if available, or username, or construct from ID
        if chat.invite_link:
            _channel_link_cache = chat.invite_link
        elif chat.username:
            _channel_link_cache = f"https://t.me/{chat.username}"
        else:
            # For private channels without username: strip -100 prefix for t.me/c/ format
            raw = str(channel_id).lstrip("-")
            if raw.startswith("100"):
                raw = raw[3:]
            _channel_link_cache = f"https://t.me/c/{raw}"
    except Exception:
        logger.warning("Failed to resolve force-sub channel link for %s", channel_id)
        raw = str(channel_id).lstrip("-")
        if raw.startswith("100"):
            raw = raw[3:]
        _channel_link_cache = f"https://t.me/c/{raw}"

    return _channel_link_cache


def force_sub(handler: Handler) -> Handler:
    """Block non-members from using the handler if FORCE_SUB is enabled.

    Requires the Pyrogram client as the first argument (`_` / `client`).
    Admins bypass the check.
    """
    @wraps(handler)
    async def wrapper(_, message: Message, *args: Any, **kwargs: Any) -> Any:
        # Feature disabled — pass through
        if not settings.force_sub_enabled or not settings.force_sub_channel_id:
            logger.debug("Force-sub: disabled, passing through")
            return await handler(_, message, *args, **kwargs)

        user = message.from_user
        if not user:
            return await handler(_, message, *args, **kwargs)

        # Admins bypass
        if user.id in settings.admin_user_ids:
            return await handler(_, message, *args, **kwargs)

        # Check membership cache first
        now = time.monotonic()
        cached = _membership_cache.get(user.id)
        if cached:
            is_cached_member, cached_at = cached
            if now - cached_at < _MEMBERSHIP_CACHE_TTL:
                if is_cached_member:
                    return await handler(_, message, *args, **kwargs)
                # Cached non-member: still show the prompt
                channel_link = await _get_channel_link(_)
                from tunedrop.app.utils.ui_utils import build_force_sub_message
                text, markup = build_force_sub_message(channel_link)
                await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
                return None

        # Query Telegram API
        try:
            member = await _.get_chat_member(settings.force_sub_channel_id, user.id)
            is_member = member is not None and member.status.name not in ("LEFT", "BANNED")
            logger.debug("Force-sub: user=%s status=%s is_member=%s", user.id, member.status.name, is_member)
        except Exception as exc:
            logger.warning("Force-sub: user=%s exception: %s", user.id, exc)
            is_member = False

        # Update cache
        _membership_cache[user.id] = (is_member, now)

        if is_member:
            return await handler(_, message, *args, **kwargs)

        channel_link = await _get_channel_link(_)
        from tunedrop.app.utils.ui_utils import build_force_sub_message
        text, markup = build_force_sub_message(channel_link)
        await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        return None

    return wrapper  # type: ignore[return-value]
