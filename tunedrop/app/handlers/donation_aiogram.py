from __future__ import annotations

import logging

from aiogram import Dispatcher, F
from aiogram.types import Message

from tunedrop.app.services.subscription import subscription_service

logger = logging.getLogger(__name__)


def register_aiogram_handlers(dp: Dispatcher) -> None:
    """Register aiogram handlers for Bot API updates (successful payments)."""
    logger.info("=== REGISTERING AIORAM DONATION HANDLERS ===")

    @dp.message(F.successful_payment)
    async def handle_successful_payment(message: Message):
        """Handle successful Stars payment from Bot API."""
        payment = message.successful_payment
        if not payment:
            logger.warning("Successful payment message but no payment object")
            return

        logger.info("=== AIORAM: Successful payment received ===")
        logger.info("Invoice payload: %s", payment.invoice_payload)
        logger.info("Total amount: %s", payment.total_amount)

        payload = payment.invoice_payload or ""
        if not payload.startswith("tunedrop_donate:"):
            logger.warning("Payment payload doesn't match our format: %s", payload)
            return

        parts = payload.split(":")
        if len(parts) < 3:
            logger.warning("Invalid donation payload: %s", payload)
            return

        try:
            paid_user_id = int(parts[1])
            stars_amount = int(parts[2])
        except (ValueError, IndexError):
            logger.warning("Failed to parse donation payload: %s", payload)
            return

        final_amount = payment.total_amount or stars_amount

        await subscription_service.record_donation(paid_user_id, final_amount)
        logger.info("Recorded donation: user %d, %d Stars", paid_user_id, final_amount)

        # Send thank you message with buttons
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❤️ Show my name", callback_data=f"donor_public:{final_amount}"))
        builder.row(InlineKeyboardButton(text="🙈 Stay anonymous", callback_data=f"donor_anon:{final_amount}"))

        try:
            await message.answer(
                text=(
                    f"<b>❤️ Thank you for your support!</b>\n\n"
                    f"You donated <b>{final_amount}</b> ⭐\n"
                    "Your generosity helps keep TuneDrop free for everyone.\n\n"
                    "<i>Want to be mentioned in our channel?</i>"
                ),
                parse_mode="HTML",
                reply_markup=builder.as_markup(),
            )
            logger.info("Sent thank you message to user %d", message.from_user.id)
        except Exception as e:
            logger.error("Failed to send thank you message: %s", e, exc_info=True)

    @dp.callback_query(F.data.startswith("donor_"))
    async def handle_donor_choice(callback_query):
        """Handle donor visibility choice (public/anonymous)."""
        from tunedrop.app.core.config import settings
        from tunedrop.app.core.client import get_pyrogram_client
        from pyrogram.enums import ParseMode
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        logger.info("=== AIORAM: Donor visibility callback ===")
        logger.info("Callback data: %s", callback_query.data)

        parts = callback_query.data.split(":")
        choice = parts[0].split("_")[1]  # "public" or "anon"
        stars = int(parts[1])

        logger.info("Choice: %s, Stars: %d", choice, stars)

        # Check if donation notifications are enabled
        if not settings.donation_notifications_enabled:
            logger.info("Donation notifications disabled")
            await callback_query.answer("Thanks for your support!")
            return

        # Use dedicated notification channel or fall back to force-sub channel
        channel_id = settings.donation_notifications_channel_id or settings.force_sub_channel_id
        if not channel_id:
            logger.warning("No notification channel configured")
            await callback_query.answer("Thanks for your support!")
            return

        try:
            # Get channel info
            pyrogram_client = get_pyrogram_client()
            if not pyrogram_client:
                logger.error("Pyrogram client not available")
                await callback_query.answer("Error processing your request")
                return

            channel = await pyrogram_client.get_chat(channel_id)
            channel_username = channel.username

            if choice == "public":
                user = callback_query.from_user
                first_name = (user.first_name or "").strip()
                display = f'<a href="tg://user?id={user.id}">{first_name}</a>'
            else:
                display = "a kind supporter"

            notif_text = (
                f"✨ <b>New supporter!</b>\n\n"
                f"{display} donated <b>{stars}</b> ⭐\n\n"
                "Every donation helps keep the music free for everyone."
            )

            # Use Pyrogram's InlineKeyboardMarkup - only Support button
            buttons = [
                [InlineKeyboardButton("⭐ Support TuneDrop", callback_data="donate_trigger")]
            ]

            notif_markup = InlineKeyboardMarkup(buttons)

            logger.info("Sending donation thank you to channel %s", channel_id)

            notif_msg = await pyrogram_client.send_message(
                chat_id=channel_id,
                text=notif_text,
                parse_mode=ParseMode.HTML,
                reply_markup=notif_markup,
            )
            logger.info("Donation thank you message sent: msg_id=%d", notif_msg.id)

            # Pin the message in the channel
            try:
                await pyrogram_client.pin_chat_message(
                    chat_id=channel_id,
                    message_id=notif_msg.id,
                    disable_notification=True,
                )
                logger.info("Pinned donation message in channel")
            except Exception as e:
                logger.warning("Failed to pin donation notification: %s", e, exc_info=True)

            # Send confirmation message BEFORE deleting the old one
            await callback_query.message.answer(
                text="✅ <b>Thank you!</b>\n\n"
                     "Your support has been posted to our channel.\n"
                     "You help keep TuneDrop free for everyone! ❤️",
                parse_mode="HTML",
            )

            # Delete the thank you message with buttons
            try:
                await callback_query.message.delete()
                logger.info("Deleted thank you message with buttons")
            except Exception as e:
                logger.warning("Failed to delete thank you message: %s", e)

            await callback_query.answer("Thank you! Posted to channel!")
        except Exception as e:
            logger.error("Failed to post donation notification: %s", e, exc_info=True)
            await callback_query.answer("Thanks for your support!", show_alert=True)
