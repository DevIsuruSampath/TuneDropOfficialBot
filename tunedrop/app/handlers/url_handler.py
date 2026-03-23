from __future__ import annotations

from pyrogram import Client, filters

from tunedrop.app.services.downloader import DownloadRequest, download_manager
from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.decorators import once_per_message
from tunedrop.app.utils.filters import music_input
from tunedrop.app.utils.validators import classify_input, looks_like_url

_UNSUPPORTED_MSG = (
    "Only YouTube Music and Spotify URLs are supported.\n\n"
    "Use /song <query> to search for music."
)


def register(app: Client) -> None:
    @app.on_message(filters.text & ~filters.command(["start", "help", "song", "myfiles", "cancel"]) & music_input)
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
        & ~filters.command(["start", "help", "song", "myfiles", "cancel"])
        & ~music_input
        & filters.create(lambda _, __, m: bool(looks_like_url(m.text or "")))
    )
    @once_per_message
    async def unsupported_url_handler(client: Client, message):
        await message.reply_text(_UNSUPPORTED_MSG)
