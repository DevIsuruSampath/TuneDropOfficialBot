from __future__ import annotations

import asyncio
import logging

# aiogram changes the event loop policy to a uvloop-backed one at import time.
# pyrogram's sync module calls get_event_loop() which fails under uvloop policy
# because it doesn't auto-create loops. Ensure a loop exists before pyrogram imports.
from aiogram import Bot
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client
from pyrogram.types import BotCommand

from tunedrop.app.core.config import settings

logger = logging.getLogger(__name__)

_aiogram_bot: Bot | None = None
_pyrogram_client: Client | None = None


def set_pyrogram_client(client: Client) -> None:
    """Store a reference to the running Pyrogram client for use by the web server."""
    global _pyrogram_client
    _pyrogram_client = client


def get_pyrogram_client() -> Client | None:
    """Return the running Pyrogram client, or None if not started."""
    return _pyrogram_client


def get_aiogram_bot() -> Bot:
    """Return a shared aiogram Bot instance for Bot API calls (invoice, drain, etc)."""
    global _aiogram_bot
    if _aiogram_bot is None:
        _aiogram_bot = Bot(token=settings.bot_token)
    return _aiogram_bot


async def close_aiogram_bot() -> None:
    """Close the aiogram Bot session (call on shutdown)."""
    global _aiogram_bot
    if _aiogram_bot is not None:
        await _aiogram_bot.session.close()
        _aiogram_bot = None


def create_bot_client() -> Client:
    return Client(
        name=settings.bot_session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        workdir=str(settings.data_dir),
        in_memory=False,
    )


async def _drain_bot_api_updates() -> None:
    """Clear pending Bot API getUpdates and set empty webhook to prevent accumulation."""
    try:
        bot = get_aiogram_bot()
        for _ in range(20):
            updates = await bot.get_updates(limit=100, timeout=0)
            if not updates:
                break
            last_id = updates[-1].update_id
            await bot.get_updates(offset=last_id + 1, limit=1, timeout=0)
            logger.info("Drained %d pending Bot API updates (last_id=%s)", len(updates), last_id)

        await bot.set_webhook("")
        logger.info("Set empty webhook (drain)")
    except Exception:
        logger.debug("Failed to drain Bot API updates", exc_info=True)


def register_handlers(app: Client) -> None:
    from tunedrop.app.handlers import (
        callback_handler,
        donation_handler,
        errors,
        playlist_handler,
        song_command,
        start,
        url_handler,
    )

    start.register(app)
    song_command.register(app)
    url_handler.register(app)
    playlist_handler.register(app)
    callback_handler.register(app)
    donation_handler.register(app)  # includes /admin, /donation, all admin callbacks
    errors.register(app)


async def register_bot_commands(app: Client) -> None:
    await app.set_bot_commands(
        [
            BotCommand("start", "Show the welcome message"),
            BotCommand("help", "Show usage instructions"),
            BotCommand("song", "Search and download a song"),
            BotCommand("myfiles", "List your recent playlist ZIP links"),
            BotCommand("cancel", "Cancel the current task"),
            BotCommand("donation", "Support TuneDrop with Stars"),
        ]
    )
