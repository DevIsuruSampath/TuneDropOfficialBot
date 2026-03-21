from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from tunedrop.app.core.constants import HELP_TEXT, WELCOME_TEXT
from tunedrop.app.utils.decorators import once_per_message
from tunedrop.app.utils.ui_utils import build_welcome_keyboard


def register(app: Client) -> None:
    _welcome_markup = build_welcome_keyboard()

    @app.on_message(filters.command("start"))
    @once_per_message
    async def start_handler(_, message):
        await message.reply_text(WELCOME_TEXT, reply_markup=_welcome_markup, parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("help"))
    @once_per_message
    async def help_handler(_, message):
        await message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

    @app.on_callback_query(filters.regex("^show_help$"))
    async def help_callback(_, callback_query):
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(HELP_TEXT, parse_mode=ParseMode.HTML)
        except Exception:
            await callback_query.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

    @app.on_callback_query(filters.regex("^show_search$"))
    async def search_callback(_, callback_query):
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                "<b>Search</b>\n\n"
                "Type <code>/song</code> <i>name</i>\n\n"
                "<i>e.g. /song Blinding Lights</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
