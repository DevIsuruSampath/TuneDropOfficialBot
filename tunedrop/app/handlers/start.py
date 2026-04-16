from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.core.config import settings
from tunedrop.app.core.constants import HELP_TEXT, WELCOME_TEXT
from tunedrop.app.utils.decorators import force_sub, once_per_message, _check_sub_cache, _set_sub_cache, _get_channel_link
from tunedrop.app.utils.ui_utils import build_welcome_keyboard, build_back_keyboard, build_force_sub_message, escape_html, format_expiry

logger = logging.getLogger(__name__)

_DONATION_PRESETS = [50, 100, 250, 500]
_PRO_PRICE = 500
_PRO_DAYS = 30
_DONATION_TEXT = (
    "<b>❤️ Support TuneDrop</b>\n\n"
    "TuneDrop is free and community-powered.\n"
    "Your donations keep the servers running and the music flowing.\n\n"
    f"<b>⭐ Pro — our thank-you for {_PRO_PRICE}+ Stars</b>\n"
    "• 📥 Instant delivery — audio in Telegram\n"
    "• 🚫 No ads — clean download pages\n"
    "• ⚡ Priority queue — faster when busy\n\n"
    "Pro time stacks if you're already Pro.\n\n"
    "Choose an amount or enter a custom value:\n"
    "<i>View your account → /account</i>"
)


def _build_donation_keyboard(*, history: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for amount in _DONATION_PRESETS:
        label = f"{amount} Stars \u2b50" if amount != _PRO_PRICE else f"⭐ Pro ({amount} Stars)"
        row.append(InlineKeyboardButton(
            label,
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
    buttons.append([
        InlineKeyboardButton("\u2b05 Back", callback_data="back_to_start"),
    ])
    if history:
        buttons[-1].insert(0, InlineKeyboardButton("📜 History", callback_data="show_donation_history"))
    return InlineKeyboardMarkup(buttons)


async def _build_donation_text_for_user(user_id: int) -> str:
    """Build donation text with user's Pro status and recent donations."""
    from tunedrop.app.services.subscription import subscription_service
    from tunedrop.app.utils.ui_utils import format_expiry

    is_pro = await subscription_service.is_pro(user_id)
    header = ""
    if is_pro:
        user = await subscription_service.get_or_create_user(user_id)
        pro_until = user.get("pro_until")
        header = f"<b>⭐ You're Pro! ({format_expiry(pro_until)})</b>\n\n"

    # Fetch last 5 donations
    donations = await subscription_service.get_donation_history(user_id, limit=5)
    history_lines = ""
    if donations:
        history_lines = "\n📜 <b>Recent donations</b>\n"
        for d in donations:
            stars = d.get("stars", 0)
            created = d.get("created_at")
            date_str = created.strftime("%b %d") if created else "?"
            history_lines += f"• {stars:,} ⭐ — {date_str}\n"

    return header + _DONATION_TEXT + history_lines


def register(app: Client) -> None:
    _welcome_markup = build_welcome_keyboard()

    @app.on_message(filters.command("start"))
    @force_sub
    @once_per_message
    async def start_handler(_, message):
        # Deep link: /start donation → show donation page
        if len(message.command) > 1 and message.command[1] == "donation":
            text = await _build_donation_text_for_user(message.from_user.id)
            await message.reply_text(
                text,
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

    @app.on_callback_query(filters.regex("^check_sub$"))
    async def check_sub_callback(_, callback_query):
        """Re-check if user has joined the force-sub channel after tapping 'Try Again'."""
        if not settings.force_sub_enabled or not settings.force_sub_channel_id:
            await callback_query.answer("Welcome!", show_alert=False)
            try:
                await callback_query.message.delete()
            except Exception:
                pass
            return

        user = callback_query.from_user
        if not user or user.id in settings.admin_user_ids:
            await callback_query.answer("Welcome back!", show_alert=False)
            try:
                await callback_query.message.delete()
            except Exception:
                pass
            return

        # Check membership
        try:
            member = await _.get_chat_member(settings.force_sub_channel_id, user.id)
            is_member = member is not None and member.status.name not in ("LEFT", "BANNED")
        except Exception:
            is_member = False

        _set_sub_cache(user.id, is_member)

        if is_member:
            await callback_query.answer("✅ Verified! Welcome to TuneDrop.", show_alert=False)
            try:
                await callback_query.message.delete()
            except Exception:
                pass
        else:
            channel_link = await _get_channel_link(_)
            text, markup = build_force_sub_message(channel_link)
            await callback_query.answer("You haven't joined yet. Please join first.", show_alert=True)
            try:
                await callback_query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            except Exception:
                pass

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
            "<b>🔍 Search for a song</b>\n\n"
            "Type <code>/song</code> followed by the name.\n\n"
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
        user_id = callback_query.from_user.id
        text = await _build_donation_text_for_user(user_id)
        try:
            await msg.edit_text(
                text,
                reply_markup=_build_donation_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.debug("Failed to edit donation callback", exc_info=True)
            if msg:
                await msg.reply_text(
                    text,
                    reply_markup=_build_donation_keyboard(),
                    parse_mode=ParseMode.HTML,
                )

    @app.on_callback_query(filters.regex("^show_donation_history$"))
    async def donation_history_callback(_, callback_query):
        user_id = callback_query.from_user.id
        from tunedrop.app.services.subscription import subscription_service

        donations = await subscription_service.get_donation_history(user_id, limit=20)
        if not donations:
            await callback_query.answer("No donations yet.", show_alert=True)
            return

        total_stars = 0
        lines = ["<b>📜 Donation History</b>\n"]
        for d in donations:
            stars = d.get("stars", 0)
            total_stars += stars
            created = d.get("created_at")
            date_str = created.strftime("%b %d, %Y") if created else "?"
            lines.append(f"• {stars:,} ⭐ — <i>{date_str}</i>")

        lines.append(f"\n<b>Total: {total_stars:,} ⭐</b>")

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❤️ Donate Now", callback_data="show_donation"),
                InlineKeyboardButton("⬅ Back", callback_data="back_to_start"),
            ],
        ])
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                "\n".join(lines),
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.debug("Failed to edit donation history callback", exc_info=True)

    @app.on_callback_query(filters.regex("^back_to_start$"))
    async def back_callback(_, callback_query):
        await callback_query.answer()
        msg = callback_query.message
        try:
            await msg.edit_text(WELCOME_TEXT, reply_markup=_welcome_markup, parse_mode=ParseMode.HTML)
        except Exception:
            logger.debug("Failed to edit back callback", exc_info=True)

    @app.on_message(filters.command("account") & filters.private)
    @once_per_message
    async def account_handler(_, message):
        from tunedrop.app.services.subscription import subscription_service

        user = message.from_user
        user_id = user.id

        # Get subscription + user record
        is_pro = await subscription_service.is_pro(user_id)
        user_record = await subscription_service.get_or_create_user(user_id)
        total_stars = user_record.get("stars_paid", 0)

        # Status line
        if is_pro:
            pro_until = user_record.get("pro_until")
            status = f"⭐ Pro · {format_expiry(pro_until)}"
        else:
            status = "🎧 Free"

        # Build account info
        lines = [
            "<b>🎵 TuneDrop Account</b>",
            "",
            status,
            f"🆔 <code>{user_id}</code>",
            f"👤 <a href=\"tg://user?id={user_id}\">{escape_html(user.first_name or 'Unknown')}</a>",
        ]

        if user.username:
            lines.append(f"🔗 @{user.username}")

        lines.append("")
        lines.append(f"⭐ {total_stars:,} Stars donated")

        text = "\n".join(lines)

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❤️ Support TuneDrop", callback_data="show_donation")],
        ])
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
