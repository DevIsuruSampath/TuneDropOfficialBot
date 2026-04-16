from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from tunedrop.app.services.downloader import DownloadRequest, download_manager
from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.decorators import force_sub, once_per_message, rate_limit
from tunedrop.app.utils.filters import music_input
from tunedrop.app.utils.validators import classify_input, looks_like_url

_UNSUPPORTED_MSG = (
    "🔗 <b>Unsupported link</b>\n\n"
    "Only Spotify and YouTube Music links are supported.\n"
    "Use <code>/song</code> + name to search instead."
)


def register(app: Client) -> None:
    _EXCLUDED_COMMANDS = ["start", "help", "song", "myfiles", "cancel", "admin", "ads", "stats", "pro", "grantpro", "revokepro", "userinfo"]

    @app.on_message(filters.text & ~filters.command(_EXCLUDED_COMMANDS) & music_input)
    @force_sub
    @rate_limit
    @once_per_message
    async def url_handler(client: Client, message):
        raw = (message.text or "").strip()
        request = DownloadRequest.from_input(
            user_id=message.from_user.id if message.from_user else 0,
            chat_id=message.chat.id,
            source=raw,
            input_type=classify_input(raw),
        )
        await task_registry.start_download(client, message, request, download_manager)

    @app.on_message(
        filters.text
        & ~filters.command(_EXCLUDED_COMMANDS)
        & ~music_input
        & filters.create(lambda _, __, m: bool(looks_like_url(m.text or "")))
    )
    @once_per_message
    async def unsupported_url_handler(client: Client, message):
        await message.reply_text(_UNSUPPORTED_MSG, parse_mode=ParseMode.HTML)
