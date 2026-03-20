from __future__ import annotations

from pyrogram import Client, filters

from app.services.downloader import DownloadRequest, download_manager
from app.services.progress import task_registry
from app.utils.filters import music_input
from app.utils.validators import classify_input, is_supported_url


def register(app: Client) -> None:
    @app.on_message(filters.text & ~filters.command(["start", "help", "song", "myfiles", "cancel"]) & music_input)
    async def url_handler(client: Client, message):
        raw = (message.text or "").strip()
        if not is_supported_url(raw):
            await message.reply_text("Unsupported input. Send a Spotify/YouTube URL or use /song.")
            return

        request = DownloadRequest.from_input(
            user_id=message.from_user.id if message.from_user else 0,
            chat_id=message.chat.id,
            source=raw,
            input_type=classify_input(raw),
        )
        await task_registry.start_download(client, message, request, download_manager)
