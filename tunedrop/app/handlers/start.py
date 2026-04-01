from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from tunedrop.app.core.config import settings
from tunedrop.app.core.constants import HELP_TEXT, WELCOME_TEXT
from tunedrop.app.utils.decorators import force_sub, once_per_message
from tunedrop.app.utils.ui_utils import build_welcome_keyboard, build_back_keyboard

logger = logging.getLogger(__name__)


def register(app: Client) -> None:
    _welcome_markup = build_welcome_keyboard()

    @app.on_message(filters.command("start"))
    @force_sub
    @once_per_message
    async def start_handler(_, message):
        if settings.welcome_image:
            try:
                await message.reply_photo(
                    settings.welcome_image,
                    caption=WELCOME_TEXT,
                    reply_markup=_welcome_markup,
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception:
                logger.warning("Failed to send welcome image, falling back to text", exc_info=True)
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
            await msg.edit_text(HELP_TEXT, reply_markup=build_back_keyboard(), parse_mode=ParseMode.HTML)
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
            await msg.edit_text(text, reply_markup=build_back_keyboard(), parse_mode=ParseMode.HTML)
        except Exception:
            logger.debug("Failed to edit search callback, falling back to reply", exc_info=True)
            if msg:
                await msg.reply_text(text, parse_mode=ParseMode.HTML)

    @app.on_callback_query(filters.regex("^back_to_start$"))
    async def back_callback(_, callback_query):
        await callback_query.answer()
        msg = callback_query.message
        try:
            await msg.edit_text(WELCOME_TEXT, reply_markup=_welcome_markup, parse_mode=ParseMode.HTML)
        except Exception:
            logger.debug("Failed to edit back callback", exc_info=True)
