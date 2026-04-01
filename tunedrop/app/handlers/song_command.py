from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from tunedrop.app.services.downloader import DownloadRequest, download_manager
from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.decorators import force_sub, once_per_message, rate_limit
from tunedrop.app.utils.helpers import command_argument


def register(app: Client) -> None:
    @app.on_message(filters.command("song"))
    @force_sub
    @rate_limit
    @once_per_message
    async def song_handler(client: Client, message):
        query = command_argument(message)
        if not query:
            await message.reply_text(
                "<code>/song</code> <i>name</i>\n\n"
                "<i>e.g. /song Blinding Lights</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        request = DownloadRequest.from_search(
            user_id=message.from_user.id if message.from_user else 0,
            chat_id=message.chat.id,
            source=query,
        )
        await task_registry.start_download(client, message, request, download_manager)
