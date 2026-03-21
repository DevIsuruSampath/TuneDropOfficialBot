from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.errors import StopPropagation
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.core.constants import HELP_TEXT, WELCOME_TEXT
from tunedrop.app.utils.filters import fresh


def register(app: Client) -> None:
    _help_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Help", callback_data="show_help")]])

    @app.on_message(fresh & filters.command("start"))
    async def start_handler(_, message):
        await message.reply_text(WELCOME_TEXT, reply_markup=_help_markup)
        raise StopPropagation

    @app.on_message(fresh & filters.command("help"))
    async def help_handler(_, message):
        await message.reply_text(HELP_TEXT)
        raise StopPropagation

    @app.on_callback_query(filters.regex("^show_help$"))
    async def help_callback(_, callback_query):
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(HELP_TEXT)
        except Exception:
            await callback_query.message.reply_text(HELP_TEXT)
