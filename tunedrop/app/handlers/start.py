from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.core.config import settings
from tunedrop.app.core.constants import HELP_TEXT, WELCOME_TEXT
from tunedrop.app.utils.decorators import force_sub, once_per_message
from tunedrop.app.utils.ui_utils import build_welcome_keyboard, build_back_keyboard

logger = logging.getLogger(__name__)

_DONATION_PRESETS = [50, 100, 250, 500]
_DONATION_TEXT = (
    "<b>Support TuneDrop</b>\n\n"
    "Help keep TuneDrop free and fast for everyone.\n"
    "Your donation covers server costs and development.\n\n"
    "Choose an amount or enter a custom value:"
)


def _build_donation_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for amount in _DONATION_PRESETS:
        row.append(InlineKeyboardButton(
            f"{amount} Stars \u2b50",
            callback_data=f"donate_{amount}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(
        "Custom Amount \u270f\ufe0f",
        callback_data="donate_custom",
    )])
    buttons.append([InlineKeyboardButton(
        "\u2b05 Back",
        callback_data="back_to_start",
    )])
    return InlineKeyboardMarkup(buttons)


def register(app: Client) -> None:
    _welcome_markup = build_welcome_keyboard()

    @app.on_message(filters.command("start"))
    @force_sub
    @once_per_message
    async def start_handler(_, message):
        # Deep link: /start donation → show donation page
        if len(message.command) > 1 and message.command[1] == "donation":
            await message.reply_text(
                _DONATION_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=_build_donation_keyboard(),
            )
            return

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
            "<b>🎵 Search</b>\n\n"
            "Type <code>/song</code> + song name\n\n"
            "<i>e.g. /song Blinding Lights</i>"
        )
        msg = callback_query.message
        try:
            await msg.edit_text(text, reply_markup=build_back_keyboard(), parse_mode=ParseMode.HTML)
        except Exception:
            logger.debug("Failed to edit search callback, falling back to reply", exc_info=True)
            if msg:
                await msg.reply_text(text, parse_mode=ParseMode.HTML)

    @app.on_callback_query(filters.regex("^show_donation$"))
    async def donation_callback(_, callback_query):
        await callback_query.answer()
        msg = callback_query.message
        try:
            await msg.edit_text(
                _DONATION_TEXT,
                reply_markup=_build_donation_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.debug("Failed to edit donation callback", exc_info=True)
            if msg:
                await msg.reply_text(
                    _DONATION_TEXT,
                    reply_markup=_build_donation_keyboard(),
                    parse_mode=ParseMode.HTML,
                )

    @app.on_callback_query(filters.regex("^back_to_start$"))
    async def back_callback(_, callback_query):
        await callback_query.answer()
        msg = callback_query.message
        try:
            await msg.edit_text(WELCOME_TEXT, reply_markup=_welcome_markup, parse_mode=ParseMode.HTML)
        except Exception:
            logger.debug("Failed to edit back callback", exc_info=True)
