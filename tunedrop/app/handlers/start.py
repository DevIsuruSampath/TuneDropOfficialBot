from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from tunedrop.app.core.constants import HELP_TEXT, WELCOME_TEXT
from tunedrop.app.utils.decorators import once_per_message
from tunedrop.app.utils.ui_utils import build_welcome_keyboard

logger = logging.getLogger(__name__)


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
        msg = callback_query.message
        try:
            await msg.edit_text(HELP_TEXT, parse_mode=ParseMode.HTML)
        except Exception:
            logger.debug("Failed to edit help callback, falling back to reply", exc_info=True)
            if msg:
                await msg.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

    @app.on_callback_query(filters.regex("^show_search$"))
    async def search_callback(_, callback_query):
        await callback_query.answer()
        text = (
            "<b>Search</b>\n\n"
            "Type <code>/song</code> <i>name</i>\n\n"
            "<i>e.g. /song Blinding Lights</i>"
        )
        msg = callback_query.message
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.debug("Failed to edit search callback, falling back to reply", exc_info=True)
            if msg:
                await msg.reply_text(text, parse_mode=ParseMode.HTML)
