from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.core.config import settings
from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.decorators import admin_only, once_per_message


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Ads", callback_data="show_ads"),
        ],
    ])


def _ads_keyboard() -> InlineKeyboardMarkup:
    state = "ON" if settings.ads_enabled else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ON", callback_data="ads_on"),
            InlineKeyboardButton("OFF", callback_data="ads_off"),
        ],
        [InlineKeyboardButton("Back", callback_data="back_admin")],
    ])


def register(app: Client) -> None:
    @app.on_message(filters.command("stats"))
    @admin_only
    @once_per_message
    async def stats_handler(_, message):
        active = task_registry.active_count
        users = len(task_registry._user_tasks)
        await message.reply_text(f"Active tasks: {active}\nUsers: {users}")

    @app.on_message(filters.command("admin"))
    @admin_only
    @once_per_message
    async def admin_handler(_, message):
        await message.reply_text(
            "<b>Admin Panel</b>",
            reply_markup=_admin_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("ads"))
    @admin_only
    @once_per_message
    async def ads_handler(_, message):
        state = "ON" if settings.ads_enabled else "OFF"
        await message.reply_text(
            f"<b>Ads</b>\n\nStatus: <code>{state}</code>",
            reply_markup=_ads_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_callback_query(filters.regex("^show_ads$"))
    @admin_only
    async def ads_status_callback(_, callback_query):
        await callback_query.answer()
        state = "ON" if settings.ads_enabled else "OFF"
        text = f"<b>Ads</b>\n\nStatus: <code>{state}</code>"
        try:
            await callback_query.message.edit_text(text, reply_markup=_ads_keyboard(), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    @app.on_callback_query(filters.regex("^ads_(on|off)$"))
    @admin_only
    async def ads_toggle_callback(_, callback_query):
        action = callback_query.data.split("_", 1)[1]
        settings.ads_enabled = action == "on"
        state = "ON" if settings.ads_enabled else "OFF"
        text = f"<b>Ads</b>\n\nStatus: <code>{state}</code>"
        try:
            await callback_query.message.edit_text(text, reply_markup=_ads_keyboard(), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    @app.on_callback_query(filters.regex("^back_admin$"))
    @admin_only
    async def back_admin_callback(_, callback_query):
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                "<b>Admin Panel</b>", reply_markup=_admin_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
