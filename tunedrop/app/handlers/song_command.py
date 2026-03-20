from __future__ import annotations

from pyrogram import Client, filters

from tunedrop.app.services.downloader import DownloadRequest, download_manager
from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.helpers import command_argument


def register(app: Client) -> None:
    @app.on_message(filters.command("song"))
    async def song_handler(client: Client, message):
        query = command_argument(message)
        if not query:
            await message.reply_text("Usage: /song <song name>")
            return

        request = DownloadRequest.from_search(
            user_id=message.from_user.id if message.from_user else 0,
            chat_id=message.chat.id,
            source=query,
        )
        await task_registry.start_download(client, message, request, download_manager)
