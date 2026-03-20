from __future__ import annotations

from pyrogram import Client, filters

from tunedrop.app.core.constants import HELP_TEXT, WELCOME_TEXT


def register(app: Client) -> None:
    @app.on_message(filters.command("start"))
    async def start_handler(_, message):
        await message.reply_text(WELCOME_TEXT)

    @app.on_message(filters.command("help"))
    async def help_handler(_, message):
        await message.reply_text(HELP_TEXT)
