from __future__ import annotations

import asyncio
import logging

# aiogram changes the event loop policy to a uvloop-backed one at import time.
# pyrogram's sync module calls get_event_loop() which fails under uvloop policy
# because it doesn't auto-create loops. Ensure a loop exists before pyrogram imports.
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client
from pyrogram.types import BotCommand

from tunedrop.app.core.config import settings

logger = logging.getLogger(__name__)

_aiogram_bot: Bot | None = None
_aiogram_dp: Dispatcher | None = None
_pyrogram_client: Client | None = None
_all_clients: list[Client] = []


def set_pyrogram_client(client: Client) -> None:
    """Store a reference to the primary Pyrogram client."""
    global _pyrogram_client
    _pyrogram_client = client


def get_pyrogram_client() -> Client | None:
    """Return the primary Pyrogram client (for web server, aiogram callbacks, cache uploads)."""
    return _pyrogram_client


def get_all_clients() -> list[Client]:
    """Return all running Pyrogram clients (primary first)."""
    return _all_clients


def get_client_by_index(index: int) -> Client | None:
    """Return a client by its index in get_all_clients(). Primary bot is index 0."""
    clients = _all_clients
    if 0 <= index < len(clients):
        return clients[index]
    return None


def get_client_index(client: Client) -> int:
    """Return the index of a client in get_all_clients(). Primary bot is index 0."""
    for i, c in enumerate(_all_clients):
        if c is client:
            return i
    return 0


def set_all_clients(clients: list[Client]) -> None:
    global _all_clients
    _all_clients = clients


def get_aiogram_bot() -> Bot:
    """Return a shared aiogram Bot instance for Bot API calls (invoice, drain, etc)."""
    global _aiogram_bot
    if _aiogram_bot is None:
        _aiogram_bot = Bot(token=settings.bot_token)
    return _aiogram_bot


def get_aiogram_dispatcher() -> Dispatcher:
    """Return a shared aiogram Dispatcher for handling Bot API updates."""
    global _aiogram_dp
    if _aiogram_dp is None:
        _aiogram_dp = Dispatcher(storage=MemoryStorage())
    return _aiogram_dp


async def close_aiogram_bot() -> None:
    """Close the aiogram Bot session (call on shutdown)."""
    global _aiogram_bot
    if _aiogram_bot is not None:
        await _aiogram_bot.session.close()
        _aiogram_bot = None


def create_bot_client() -> Client:
    """Create the primary Pyrogram bot client from settings.bot_token."""
    return Client(
        name=settings.bot_session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        workdir=str(settings.data_dir),
        in_memory=False,
    )


def create_bot_client_with_token(bot_token: str, session_suffix: str = "") -> Client:
    """Create a Pyrogram bot client with an explicit token (for secondary bots)."""
    return Client(
        name=f"{settings.bot_session_name}{session_suffix}",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=bot_token,
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
    """Register download-related handlers on any client."""
    from tunedrop.app.handlers import (
        callback_handler,
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
    errors.register(app)


def register_primary_only_handlers(app: Client) -> None:
    """Register donation/admin/payment handlers on primary client only."""
    from tunedrop.app.handlers import donation_handler
    donation_handler.register(app)


async def register_bot_commands(app: Client) -> None:
    commands = [
        BotCommand("start", "Welcome & main menu"),
        BotCommand("help", "How to use"),
        BotCommand("song", "Search & download a song"),
        BotCommand("myfiles", "Your recent downloads"),
        BotCommand("account", "Account & Pro status"),
        BotCommand("cancel", "Cancel current task"),
        BotCommand("donation", "Support TuneDrop ⭐"),
    ]
    if settings.admin_user_ids:
        commands.append(BotCommand("admin", "Admin panel"))
    await app.set_bot_commands(commands)
