from __future__ import annotations

from pyrogram import Client, filters
from pyrogram import StopPropagation

from tunedrop.app.services.downloader import DownloadRequest, download_manager
from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.filters import fresh
from tunedrop.app.utils.helpers import command_argument


def register(app: Client) -> None:
    @app.on_message(fresh & filters.command("song"))
    async def song_handler(client: Client, message):
        query = command_argument(message)
        if not query:
            await message.reply_text(
                "\U0001f3b5 Usage: /song <song name>\n\n"
                "Example: /song Blinding Lights"
            )
            raise StopPropagation

        request = DownloadRequest.from_search(
            user_id=message.from_user.id if message.from_user else 0,
            chat_id=message.chat.id,
            source=query,
        )
        await task_registry.start_download(client, message, request, download_manager)
        raise StopPropagation
