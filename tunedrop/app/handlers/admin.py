from __future__ import annotations

from pyrogram.enums import ParseMode
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from tunedrop.app.core.config import settings
from tunedrop.app.core.database import get_database
from tunedrop.app.services.progress import task_registry
from tunedrop.app.services.subscription import subscription_service


def admin_keyboard() -> InlineKeyboardMarkup:
    ads_state = "ON" if settings.ads_enabled else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("User Info", callback_data="pro_info"),
            InlineKeyboardButton("Stats", callback_data="show_stats"),
        ],
        [
            InlineKeyboardButton(f"Ads: {ads_state}", callback_data="show_ads"),
        ],
    ])


def ads_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ON", callback_data="ads_on"),
            InlineKeyboardButton("OFF", callback_data="ads_off"),
        ],
        [InlineKeyboardButton("Back", callback_data="back_admin")],
    ])


async def build_admin_text() -> str:
    active = task_registry.active_count
    queued = task_registry.queued_count
    ads_state = "ON" if settings.ads_enabled else "OFF"

    total_users = 0
    total_stars = 0
    try:
        db = get_database()
        total_users = await db["users"].count_documents({})
        total_stars = await subscription_service.get_total_donations()
    except Exception:
        pass

    return (
        "<b>Admin Panel</b>\n\n"
        f"<b>Tasks:</b> {active} active / {queued} queued\n"
        f"<b>Users:</b> {total_users} total\n"
        f"  \u2b50 Stars donated: {total_stars}\n"
        f"<b>Ads:</b> <code>{ads_state}</code>\n\n"
        "<i>Tap a button below. For user info, forward a message from the target user or send their ID.</i>"
    )
