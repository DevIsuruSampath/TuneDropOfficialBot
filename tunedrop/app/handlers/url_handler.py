from __future__ import annotations

from pyrogram import Client, filters

from tunedrop.app.services.downloader import DownloadRequest, download_manager
from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.filters import fresh, music_input
from tunedrop.app.utils.validators import classify_input


def register(app: Client) -> None:
    @app.on_message(fresh & filters.text & ~filters.command(["start", "help", "song", "myfiles", "cancel"]) & music_input)
    async def url_handler(client: Client, message):
        raw = (message.text or "").strip()
        request = DownloadRequest.from_input(
            user_id=message.from_user.id if message.from_user else 0,
            chat_id=message.chat.id,
            source=raw,
            input_type=classify_input(raw),
        )
        await task_registry.start_download(client, message, request, download_manager)
        raise StopPropagation
