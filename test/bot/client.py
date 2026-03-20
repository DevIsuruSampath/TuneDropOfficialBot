from __future__ import annotations

from pyrogram import Client

from config import settings


def create_bot_client() -> Client:
    return Client(
        name=settings.bot_session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        workdir=str(settings.data_dir),
        in_memory=False,
    )


def register_handlers(app: Client) -> None:
    from bot.handlers import admin, callback_handler, errors, playlist_handler, song_command, start, url_handler

    start.register(app)
    song_command.register(app)
    url_handler.register(app)
    playlist_handler.register(app)
    callback_handler.register(app)
    admin.register(app)
    errors.register(app)
