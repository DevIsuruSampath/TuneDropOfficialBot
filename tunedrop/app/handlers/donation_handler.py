from __future__ import annotations

import asyncio
import logging
import time

from aiogram.types import LabeledPrice
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.raw.functions.messages.set_bot_precheckout_results import SetBotPrecheckoutResults
from pyrogram.raw.types import UpdateBotPrecheckoutQuery
from pyrogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.core.client import get_aiogram_bot
from tunedrop.app.core.config import settings
from tunedrop.app.services.subscription import subscription_service

logger = logging.getLogger(__name__)

_admin_state: dict[int, tuple[str, int]] = {}

_DONATION_PRESETS = [50, 100, 250, 500]
_CUSTOM_AMOUNT_STATE: dict[int, int] = {}


async def _send_donation_invoice(chat_id: int, user_id: int, amount: int) -> bool:
    """Send a Stars donation invoice via aiogram."""
    bot = get_aiogram_bot()
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title="Support TuneDrop",
            description=f"Donate {amount} Stars to help keep TuneDrop free for everyone.",
            payload=f"tunedrop_donate:{user_id}:{amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"Donation — {amount} Stars", amount=amount)],
        )
        return True
    except Exception as e:
        logger.error("sendInvoice failed: %s", e)
        return False


def register(app: Client) -> None:

    # ── /admin command ──
    @app.on_message(filters.command("admin") & filters.private)
    async def admin_handler(client: Client, message):
        from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text

        user_id = message.from_user.id if message.from_user else 0
        if user_id not in settings.admin_user_ids:
            await message.reply_text("Not authorized.")
            return

        text = await build_admin_text()
        await message.reply_text(
            text,
            reply_markup=admin_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    # ── /broadcast command (reply to a message to broadcast it) ──
    @app.on_message(filters.command("broadcast") & filters.private & filters.reply)
    async def broadcast_handler(client: Client, message):
        user_id = message.from_user.id if message.from_user else 0
        if user_id not in settings.admin_user_ids:
            await message.reply_text("Not authorized.")
            return

        from tunedrop.app.core.database import get_database

        db = get_database()
        broadcast_msg = message.reply_to_message

        # Get total user count
        total_users = await db["users"].count_documents({})
        if total_users == 0:
            await message.reply_text("No users to broadcast to.")
            return

        status_msg = await message.reply_text(
            f"📢 Broadcast started...\n\nTotal users: <b>{total_users}</b>\nProgress: 0/{total_users}",
            parse_mode=ParseMode.HTML,
        )

        start_time = time.time()
        done = 0
        success = 0
        failed = 0
        failed_log: list[str] = []

        cursor = db["users"].find({}, projection={"user_id": 1, "_id": 0})
        async for user_doc in cursor:
            target_id = user_doc.get("user_id")
            if not target_id:
                continue

            try:
                await broadcast_msg.copy(chat_id=target_id)
                success += 1
            except Exception as e:
                failed += 1
                err_name = type(e).__name__
                failed_log.append(f"{target_id}: {err_name}")
                # Remove deactivated / blocked users
                if err_name in ("InputUserDeactivated", "UserIsBlocked", "PeerIdInvalid"):
                    await db["users"].delete_one({"user_id": target_id})

            done += 1

            # Update progress every 20 users
            if done % 20 == 0:
                try:
                    await status_msg.edit_text(
                        f"📢 Broadcast in progress...\n\n"
                        f"Total: <b>{total_users}</b>\n"
                        f"Done: {done}\n"
                        f"✅ Success: {success}\n"
                        f"❌ Failed: {failed}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)

        result_text = (
            f"📢 <b>Broadcast Complete</b>\n\n"
            f"Total: <b>{total_users}</b>\n"
            f"✅ Success: <b>{success}</b>\n"
            f"❌ Failed: <b>{failed}</b>\n"
            f"⏱ Time: {mins}m {secs}s"
        )

        try:
            await status_msg.edit_text(result_text, parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply_text(result_text, parse_mode=ParseMode.HTML)

        # Send failed log as file if there are failures
        if failed_log:
            import tempfile
            from pathlib import Path
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", prefix="broadcast_")
            tmp.write("\n".join(failed_log).encode())
            tmp.close()
            try:
                await message.reply_document(
                    str(tmp.name),
                    caption=f"Failed deliveries ({failed}):\n{result_text}",
                    parse_mode=ParseMode.HTML,
                )
            finally:
                Path(tmp.name).unlink(missing_ok=True)

    # ── /donation command ──
    @app.on_message(filters.command("donation") & filters.private)
    async def donation_command(client: Client, message):
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

        await message.reply_text(
            "<b>Support TuneDrop</b>\n\n"
            "Help keep TuneDrop free and fast for everyone.\n"
            "Your donation covers server costs and development.\n\n"
            "Choose an amount or enter a custom value:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Donation preset callbacks ──
    @app.on_callback_query(filters.regex(r"^donate_(\d+)$"))
    async def donate_preset_callback(client: Client, callback_query):
        user_id = callback_query.from_user.id
        amount = int(callback_query.data.split("_", 1)[1])
        try:
            ok = await _send_donation_invoice(callback_query.message.chat.id, user_id, amount)
            if not ok:
                await callback_query.answer("Failed to create payment. Try again.", show_alert=True)
                return
        except Exception:
            logger.exception("Failed to send Stars invoice to user %d", user_id)
            await callback_query.answer("Failed to create payment. Try again.", show_alert=True)
            return
        await callback_query.answer()

    # ── Donation custom amount callback ──
    @app.on_callback_query(filters.regex("^donate_custom$"))
    async def donate_custom_callback(client: Client, callback_query):
        admin_id = callback_query.from_user.id
        sent = await callback_query.message.reply_text(
            "Send the <b>amount of Stars</b> you want to donate (1-2500):",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
        )
        _CUSTOM_AMOUNT_STATE[admin_id] = sent.id
        await callback_query.answer()

    # ── Custom amount text input ──
    @app.on_message(filters.text & filters.private, group=1)
    async def custom_amount_input(_, message):
        user_id = message.from_user.id
        state_id = _CUSTOM_AMOUNT_STATE.pop(user_id, None)
        if not state_id:
            return
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            _CUSTOM_AMOUNT_STATE.pop(user_id, None)
            return
        try:
            amount = int(raw)
            if amount < 1 or amount > 2500:
                raise ValueError
        except ValueError:
            await message.reply_text(
                "Invalid amount. Please enter a number between 1 and 2500.\n"
                "Tap a button in /donation to try again.",
            )
            return

        try:
            ok = await _send_donation_invoice(message.chat.id, user_id, amount)
            if not ok:
                await message.reply_text("Failed to create payment. Try again with /donation.")
                return
        except Exception:
            logger.exception("Failed to send custom Stars invoice to user %d", user_id)
            await message.reply_text("Failed to create payment. Try again with /donation.")
            return

    # ── Admin text input handler (grant/revoke/userinfo flows) ──
    @app.on_message(filters.text & filters.private, group=2)
    async def admin_input_handler(_, message):
        admin_id = message.from_user.id if message.from_user else 0
        if admin_id not in settings.admin_user_ids:
            return
        state = _admin_state.pop(admin_id, None)
        if not state:
            return

        action, _ = state
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            return

        target_id = None
        if message.forward_from:
            target_id = message.forward_from.id
        else:
            parts = raw.split()
            for p in parts:
                try:
                    target_id = int(p)
                    break
                except ValueError:
                    continue

        if not target_id:
            await message.reply_text("Invalid input. Tap a button in /admin to try again.")
            return

        if action == "userinfo":
            info = await subscription_service.get_user_info(target_id)
            if not info:
                await message.reply_text(
                    f"No user record for <code>{target_id}</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return
            stars = info.get("stars_paid", 0)
            created = info.get("created_at", "N/A")
            await message.reply_text(
                f"<b>User {target_id}</b>\n"
                f"Stars donated: {stars}\n"
                f"Joined: <code>{created}</code>",
                parse_mode=ParseMode.HTML,
            )

    # ── Admin: Back to panel ──
    @app.on_callback_query(filters.regex("^back_admin$"))
    async def back_admin_callback(_, callback_query):
        user_id = callback_query.from_user.id
        if user_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text
        await callback_query.answer("Back to panel")
        text = await build_admin_text()
        try:
            await callback_query.message.edit_text(
                text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("edit_text failed in back_admin: %s", e)

    # ── Admin: Stats ──
    @app.on_callback_query(filters.regex("^show_stats$"))
    async def stats_callback(_, callback_query):
        user_id = callback_query.from_user.id
        if user_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text
        await callback_query.answer("Stats refreshed")
        text = await build_admin_text()
        try:
            await callback_query.message.edit_text(
                text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("edit_text failed in stats: %s", e)

    # ── Admin: Ads panel ──
    @app.on_callback_query(filters.regex("^show_ads$"))
    async def ads_panel_callback(_, callback_query):
        user_id = callback_query.from_user.id
        if user_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import ads_keyboard
        await callback_query.answer()
        state = "ON" if settings.ads_enabled else "OFF"
        text = f"<b>Ads Control</b>\n\nCurrent: <code>{state}</code>"
        try:
            await callback_query.message.edit_text(
                text, reply_markup=ads_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("edit_text failed in ads_panel: %s", e)

    # ── Admin: Ads toggle ──
    @app.on_callback_query(filters.regex("^ads_(on|off)$"))
    async def ads_toggle_callback(_, callback_query):
        user_id = callback_query.from_user.id
        if user_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import ads_keyboard
        action = callback_query.data.split("_", 1)[1]
        want_on = action == "on"
        if settings.ads_enabled == want_on:
            state = "ON" if want_on else "OFF"
            await callback_query.answer(f"Ads already {state}!", show_alert=True)
            return
        settings.ads_enabled = want_on
        state = "ON" if settings.ads_enabled else "OFF"
        text = f"<b>Ads Control</b>\n\nCurrent: <code>{state}</code>"
        await callback_query.answer(f"Ads turned {state}")
        try:
            await callback_query.message.edit_text(
                text, reply_markup=ads_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("edit_text failed in ads_toggle: %s", e)

    # ── Admin: User Info ──
    @app.on_callback_query(filters.regex("^pro_info$"))
    async def pro_info_callback(_, callback_query):
        user_id = callback_query.from_user.id
        if user_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        admin_id = callback_query.from_user.id
        sent = await callback_query.message.reply_text(
            "Send the <b>user ID</b> to look up.\n"
            "(You can also forward a message from that user)",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
        )
        _admin_state[admin_id] = ("userinfo", sent.id)
        await callback_query.answer()

    # ── Stars payment: precheckout approval ──
    @app.on_raw_update()
    async def on_raw_update(client: Client, update, users, chats):
        if isinstance(update, UpdateBotPrecheckoutQuery):
            try:
                await client.invoke(SetBotPrecheckoutResults(
                    query_id=update.query_id,
                    success=True,
                ))
            except Exception:
                logger.exception("Failed to approve precheckout query %d", update.query_id)

    # ── Stars payment: success ──
    @app.on_message(filters.successful_payment & filters.private)
    async def on_payment_success(client: Client, message):
        payment = message.successful_payment
        if not payment:
            return

        payload = payment.invoice_payload or ""
        if not payload.startswith("tunedrop_donate:"):
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

        total_stars = payment.total_amount or stars_amount
        await subscription_service.record_donation(paid_user_id, total_stars)

        await message.reply_text(
            "<b>Thank you for your support!</b> \u2764\ufe0f\n\n"
            f"You donated {total_stars} Stars.\n"
            "Your generosity helps keep TuneDrop free for everyone.",
            parse_mode=ParseMode.HTML,
        )
        logger.info("User %d donated %d Stars", paid_user_id, total_stars)
