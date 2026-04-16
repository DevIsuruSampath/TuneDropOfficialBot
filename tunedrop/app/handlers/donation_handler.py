from __future__ import annotations

import asyncio
import logging
import time

from aiogram.types import LabeledPrice
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.raw.functions.messages.set_bot_precheckout_results import SetBotPrecheckoutResults
from pyrogram.raw.types import UpdateBotPrecheckoutQuery, UpdateNewMessage
from pyrogram.raw.types.message import Message as RawMessage
from pyrogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.core.client import get_aiogram_bot
from tunedrop.app.core.config import settings
from tunedrop.app.core.database import get_database
from tunedrop.app.services.subscription import subscription_service
from tunedrop.app.utils.decorators import admin_only

logger = logging.getLogger(__name__)

_admin_state: dict[int, tuple[str, int]] = {}

_DONATION_PRESETS = [50, 100, 250, 500]
_PRO_PRICE = 500
_PRO_DAYS = 30
_DONATION_MIN = 50
_DONATION_MAX = 10000
_CUSTOM_AMOUNT_STATE: dict[int, int] = {}


def _build_cache_rebuild_text(status: dict, note: str | None = None) -> str:
    phase = status.get("phase", "scanning")
    phase_text = "Matching to Spotify..." if phase == "matching" else "Scanning channel..."
    lines = ["<b>🔄 Rebuilding Cache</b>", ""]
    if note:
        lines.append(f"<i>{note}</i>")
        lines.append("")
    lines.extend([
        phase_text,
        f"Scanned: {status.get('scanned', 0)}",
        f"Recovered: {status.get('recovered', 0)}",
        f"Existing: {status.get('existing', 0)}",
    ])
    matched = int(status.get("matched", 0) or 0)
    deduped = int(status.get("deduped", 0) or 0)
    if matched:
        lines.append(f"Matched: {matched}")
    if deduped:
        lines.append(f"Deduped: {deduped}")
    return "\n".join(lines)


async def _send_donation_invoice(chat_id: int, user_id: int, amount: int) -> bool:
    """Send a Stars donation invoice via aiogram."""
    bot = get_aiogram_bot()
    try:
        if amount >= _PRO_PRICE:
            title = f"TuneDrop Pro — {_PRO_DAYS} Days"
            description = (
                f"Support TuneDrop and get Pro as our thank-you: instant delivery, no ads, and priority downloads for {_PRO_DAYS} days."
            )
            label = f"Pro — {amount} Stars"
        else:
            title = "Support TuneDrop"
            description = f"Donate {amount} Stars to help keep TuneDrop free for everyone."
            label = f"Donation — {amount} Stars"
        await bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=f"tunedrop_donate:{user_id}:{amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=label, amount=amount)],
        )
        return True
    except Exception as e:
        logger.error("sendInvoice failed: %s", e)
        return False


def _build_user_info_text(target_id: int, info: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build user info text and keyboard with plan toggle button."""
    stars = info.get("stars_paid", 0)
    created = info.get("created_at", "N/A")
    plan = info.get("plan", "free")
    pro_until = info.get("pro_until")

    plan_text = plan.capitalize()
    if plan == "pro" and pro_until:
        from tunedrop.app.utils.ui_utils import format_expiry
        plan_text = f"Pro ({format_expiry(pro_until)})"

    text = (
        f"<b>User {target_id}</b>\n"
        f"Plan: <b>{plan_text}</b>\n"
        f"Stars donated: {stars}\n"
        f"Joined: <code>{created}</code>"
    )

    if plan == "pro":
        buttons = [[InlineKeyboardButton("Revoke Pro", callback_data=f"revoke_pro:{target_id}")]]
    else:
        buttons = [[InlineKeyboardButton("Grant Pro (30d)", callback_data=f"grant_pro:{target_id}")]]
    buttons.append([InlineKeyboardButton("Back", callback_data="back_admin")])

    return text, InlineKeyboardMarkup(buttons)


async def _send_user_info(message, target_id: int, info: dict) -> None:
    """Send user info as a new message with plan toggle button."""
    text, markup = _build_user_info_text(target_id, info)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _send_user_info_edit(message, target_id: int, info: dict) -> None:
    """Edit existing user info message with updated plan."""
    text, markup = _build_user_info_text(target_id, info)
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception:
        pass


def register(app: Client) -> None:
    logger.info("=== DONATION HANDLER REGISTERED ===")

    # ── /admin command ──
    @app.on_message(filters.command("admin") & filters.private)
    @admin_only
    async def admin_handler(client: Client, message):
        from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text

        text = await build_admin_text()
        await message.reply_text(
            text,
            reply_markup=admin_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    # ── /broadcast command (reply to a message to broadcast it) ──
    @app.on_message(filters.command("broadcast") & filters.private & filters.reply)
    @admin_only
    async def broadcast_handler(client: Client, message):
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
                await asyncio.sleep(0.05)  # Rate-limit to avoid FloodWait
            except Exception as e:
                failed += 1
                err_name = type(e).__name__
                # Handle FloodWait — sleep and retry
                from pyrogram.errors import FloodWait as FW
                if isinstance(e, FW):
                    logger.warning("Broadcast FloodWait: sleeping %ds", e.value)
                    await asyncio.sleep(e.value + 1)
                    try:
                        await broadcast_msg.copy(chat_id=target_id)
                        success += 1
                        failed -= 1
                        failed_log.pop()
                    except Exception:
                        pass
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
        from tunedrop.app.handlers.start import _build_donation_text_for_user, _build_donation_keyboard
        text = await _build_donation_text_for_user(message.from_user.id)
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_build_donation_keyboard(),
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
            f"Enter the <b>amount of Stars</b> you want to donate.\n\n"
            f"<i>Min: {_DONATION_MIN} · Max: {_DONATION_MAX:,}\n"
            f"{_PRO_PRICE}+ Stars activates Pro for {_PRO_DAYS} days.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2b05 Cancel", callback_data="donate_cancel_custom")],
            ]),
        )
        # Store both the prompt message id and the original donation message id
        _CUSTOM_AMOUNT_STATE[admin_id] = (sent.id, callback_query.message.id)
        await callback_query.answer()

    # ── Cancel custom amount input ──
    @app.on_callback_query(filters.regex("^donate_cancel_custom$"))
    async def donate_cancel_custom_callback(client: Client, callback_query):
        user_id = callback_query.from_user.id
        state = _CUSTOM_AMOUNT_STATE.pop(user_id, None)
        if state:
            # state is a tuple (prompt_msg_id, original_msg_id) or just prompt_msg_id
            prompt_msg_id = state[0] if isinstance(state, tuple) else state
            try:
                await client.delete_messages(callback_query.message.chat.id, prompt_msg_id)
            except Exception:
                pass
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        await callback_query.answer()

    # ── Custom amount text input ──
    @app.on_message(filters.text & filters.private, group=1)
    async def custom_amount_input(client: Client, message):
        user_id = message.from_user.id
        state = _CUSTOM_AMOUNT_STATE.pop(user_id, None)
        if not state:
            return
        # state is a tuple (prompt_msg_id, original_msg_id) or legacy int
        if isinstance(state, tuple):
            prompt_msg_id = state[0]
        else:
            prompt_msg_id = state
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            _CUSTOM_AMOUNT_STATE.pop(user_id, None)
            return
        try:
            amount = int(raw)
            if amount < _DONATION_MIN or amount > _DONATION_MAX:
                raise ValueError
        except ValueError:
            await message.reply_text(
                f"⚠️ Invalid amount. Enter a number between {_DONATION_MIN} and {_DONATION_MAX}.\n"
                "Use /donation to try again.",
            )
            return

        # Clean up the prompt message
        try:
            await client.delete_messages(message.chat.id, prompt_msg_id)
        except Exception:
            pass

        try:
            ok = await _send_donation_invoice(message.chat.id, user_id, amount)
            if not ok:
                await message.reply_text("⚠️ Payment failed. Try /donation again.")
                return
        except Exception:
            logger.exception("Failed to send custom Stars invoice to user %d", user_id)
            await message.reply_text("⚠️ Payment failed. Try /donation again.")
            return

    # ── Admin text input handler (grant/revoke/userinfo flows) ──
    @app.on_message(filters.text & filters.private, group=2)
    async def admin_input_handler(_, message):
        state = _admin_state.pop(message.from_user.id, None)
        if not state:
            return

        admin_id = message.from_user.id if message.from_user else 0
        if admin_id not in settings.admin_user_ids:
            return

        action, _ = state
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            return

        # Broadcast: raw is the message to send to all users
        if action == "broadcast":
            db = get_database()
            cursor = db["users"].find({}, projection={"user_id": 1, "_id": 0})
            users = await cursor.to_list(length=None)
            total = len(users)
            sent_count = 0
            fail_count = 0
            status_msg = await message.reply_text(
                f"📢 Broadcasting to {total} users...\n\nSent: 0 / {total}",
                parse_mode=ParseMode.HTML,
            )
            for i, doc in enumerate(users):
                uid = doc.get("user_id")
                if not uid:
                    continue
                try:
                    await message._client.send_message(
                        uid, raw, parse_mode=ParseMode.HTML,
                    )
                    sent_count += 1
                except Exception:
                    fail_count += 1
                # Update progress every 20 users
                if (i + 1) % 20 == 0 or (i + 1) == total:
                    try:
                        await status_msg.edit_text(
                            f"📢 Broadcasting...\n\n"
                            f"Sent: {i + 1} / {total}\n"
                            f"✅ Delivered: {sent_count}\n"
                            f"❌ Failed: {fail_count}",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                await asyncio.sleep(0.05)  # Rate limit: ~20 msg/sec
            from tunedrop.app.handlers.admin import admin_keyboard
            try:
                await status_msg.edit_text(
                    f"<b>📢 Broadcast Complete</b>\n\n"
                    f"✅ Delivered: {sent_count}\n"
                    f"❌ Failed: {fail_count}\n"
                    f"📊 Total: {total}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_keyboard(),
                )
            except Exception:
                pass
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
            await _send_user_info(message, target_id, info)

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
            # Handle MESSAGE_NOT_MODIFIED error gracefully
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed in back_admin: %s", e)

    # ── Admin: Stats ──
    @app.on_callback_query(filters.regex("^show_stats$"))
    async def stats_callback(_, callback_query):
        user_id = callback_query.from_user.id
        if user_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        try:
            from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text
            await callback_query.answer("Stats refreshed")
            text = await build_admin_text()
            try:
                await callback_query.message.edit_text(
                    text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                # Handle MESSAGE_NOT_MODIFIED error gracefully
                if "MESSAGE_NOT_MODIFIED" not in str(e):
                    logger.warning("edit_text failed in stats: %s", e)
                    await callback_query.answer("Stats refreshed (could not update message)")
        except Exception as e:
            logger.error("Stats callback error: %s", e, exc_info=True)
            await callback_query.answer("Error loading stats", show_alert=True)

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
        # Check if the message content has changed before editing
        try:
            await callback_query.message.edit_text(
                text, reply_markup=ads_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            # Handle MESSAGE_NOT_MODIFIED error gracefully
            if "MESSAGE_NOT_MODIFIED" not in str(e):
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
            # Handle MESSAGE_NOT_MODIFIED error gracefully
            if "MESSAGE_NOT_MODIFIED" not in str(e):
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

    # ── Admin: Grant Pro ──
    @app.on_callback_query(filters.regex(r"^grant_pro:(\d+)$"))
    async def grant_pro_callback(client: Client, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        target_id = int(callback_query.data.split(":")[1])
        pro_until = await subscription_service.grant_pro(target_id, days=30)
        await callback_query.answer("Pro granted for 30 days!")

        info = await subscription_service.get_user_info(target_id)
        if info:
            await _send_user_info_edit(callback_query.message, target_id, info)

    # ── Admin: Revoke Pro ──
    @app.on_callback_query(filters.regex(r"^revoke_pro:(\d+)$"))
    async def revoke_pro_callback(client: Client, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        target_id = int(callback_query.data.split(":")[1])
        await subscription_service.revoke_pro(target_id)
        await callback_query.answer("Pro revoked.")

        info = await subscription_service.get_user_info(target_id)
        if info:
            await _send_user_info_edit(callback_query.message, target_id, info)

    # ── Admin: Rebuild Cache ──
    @app.on_callback_query(filters.regex("^rebuild_cache$"))
    async def rebuild_cache_callback(client: Client, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.services.cache_service import song_cache

        status = song_cache.get_rebuild_status()
        if status.get("active"):
            await callback_query.answer("Cache rebuild already in progress.", show_alert=True)
            try:
                await callback_query.message.edit_text(
                    _build_cache_rebuild_text(status, note="A cache rebuild is already running."),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        await callback_query.answer("Scanning channel...")
        db = get_database()
        existing = await db["cached_songs"].count_documents({})
        text = f"<b>🔄 Rebuilding Cache</b>\n\nExisting entries: {existing}\nScanning channel..."
        try:
            await callback_query.message.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

        async def progress_cb(scanned, recovered, exist):
            try:
                phase = "Matching to Spotify..." if recovered > scanned else "Scanning channel..."
                await callback_query.message.edit_text(
                    f"<b>🔄 Rebuilding Cache</b>\n\n"
                    f"{phase}\n"
                    f"Scanned: {scanned}\n"
                    f"Recovered: {recovered}\n"
                    f"Existing: {exist}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        try:
            result = await song_cache.rebuild_from_channel(client, progress_cb=progress_cb)
        except Exception:
            logger.exception("Cache rebuild failed")
            from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text
            text = await build_admin_text()
            text = "<i>❌ Cache rebuild failed. Check logs and try again.</i>\n\n" + text
            try:
                await callback_query.message.edit_text(
                    text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        if result.get("already_running"):
            try:
                await callback_query.message.edit_text(
                    _build_cache_rebuild_text(result, note="A cache rebuild is already running."),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text
        note = f"✅ Cache rebuilt — scanned {result['scanned']}, recovered {result['recovered']}, existing {result['existing']}"
        if result.get('matched'):
            note += f", matched {result['matched']} to Spotify/YouTube"
        if result.get('deduped'):
            note += f", deduped {result['deduped']}"
        text = await build_admin_text()
        text = f"<i>{note}</i>\n\n" + text
        try:
            await callback_query.message.edit_text(
                text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ── Admin: Clear Cache ──
    @app.on_callback_query(filters.regex("^clear_cache$"))
    async def clear_cache_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import confirm_keyboard
        db = get_database()
        count = await db["cached_songs"].count_documents({})
        text = (
            "<b>⚠️ Clear Song Cache?</b>\n\n"
            f"This will delete all <b>{count}</b> cached song entries from MongoDB "
            "and clear in-memory caches.\n\n"
            "Next downloads will fetch fresh and re-cache to the current channel.\n\n"
            "<i>This cannot be undone.</i>"
        )
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                text, reply_markup=confirm_keyboard("clear_cache"), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed in clear_cache: %s", e)

    @app.on_callback_query(filters.regex("^clear_cache_confirm$"))
    async def clear_cache_confirm_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        db = get_database()
        # Clear MongoDB cache
        result = await db["cached_songs"].delete_many({})
        deleted = result.deleted_count

        # Clear in-memory caches
        from tunedrop.app.utils.memory_cache import _song_cache, _file_url_cache
        _song_cache.clear()
        _file_url_cache.clear()

        # Clear file_id cache used by web server
        from tunedrop.app.web.server import _fileid_cache
        _fileid_cache.clear()

        from tunedrop.app.handlers.admin import admin_keyboard, build_admin_text
        note = f"✅ Cache cleared — deleted {deleted} entries, in-memory caches purged."
        text = await build_admin_text()
        text = f"<i>{note}</i>\n\n" + text
        await callback_query.answer("Cache cleared!")
        try:
            await callback_query.message.edit_text(
                text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed after clear_cache_confirm: %s", e)

    # ── Admin: Broadcast ──
    @app.on_callback_query(filters.regex("^broadcast_start$"))
    async def broadcast_start_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        db = get_database()
        total_users = await db["users"].count_documents({})

        text = (
            "<b>📢 Broadcast Message</b>\n\n"
            f"This will send a message to all <b>{total_users}</b> users.\n\n"
            "Send the message text now (HTML supported).\n"
            "Use /cancel to abort."
        )
        sent = await callback_query.message.reply_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
        )
        _admin_state[admin_id] = ("broadcast", sent.id)
        await callback_query.answer()

    # ── Admin: Broadcast text input handler ──
    # (handled in admin_input_handler below — added "broadcast" action)

    # ── Admin: Server Status ──
    @app.on_callback_query(filters.regex("^server_status$"))
    async def server_status_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import build_server_status_text
        text = await build_server_status_text()
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="back_admin")]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed in server_status: %s", e)

    # ── Admin: Storage Manager ──
    @app.on_callback_query(filters.regex("^storage_menu$"))
    async def storage_menu_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import build_storage_text, storage_keyboard
        text = await build_storage_text()
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                text, reply_markup=storage_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed in storage_menu: %s", e)

    @app.on_callback_query(filters.regex("^clean_temp$"))
    async def clean_temp_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        await callback_query.answer("Cleaning temp files...")
        from tunedrop.app.utils.server_utils import clean_temp_files, _human_size
        result = await clean_temp_files()
        from tunedrop.app.handlers.admin import build_storage_text, storage_keyboard
        text = await build_storage_text()
        text += f"\n\n✅ <b>Cleaned:</b> {result['cleaned']} files freed ({_human_size(result['freed'])})"
        try:
            await callback_query.message.edit_text(
                text, reply_markup=storage_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed after clean_temp: %s", e)

    @app.on_callback_query(filters.regex("^clean_logs$"))
    async def clean_logs_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        await callback_query.answer("Cleaning logs...")
        from tunedrop.app.utils.server_utils import clean_logs, _human_size
        result = await clean_logs()
        from tunedrop.app.handlers.admin import build_storage_text, storage_keyboard
        text = await build_storage_text()
        text += f"\n\n✅ <b>Cleaned:</b> {result['cleaned']} log files freed ({_human_size(result['freed'])})"
        try:
            await callback_query.message.edit_text(
                text, reply_markup=storage_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed after clean_logs: %s", e)

    @app.on_callback_query(filters.regex("^clean_downloads$"))
    async def clean_downloads_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import confirm_keyboard
        from tunedrop.app.utils.server_utils import get_storage_breakdown, _human_size
        breakdown = get_storage_breakdown()
        temp_mb = sum(d["size"] for d in breakdown.values()) / (1024**2)
        text = (
            "<b>⚠️ Clean All Downloads?</b>\n\n"
            f"This will delete all files in temp/, playlists/, zip/, and songs/ ({temp_mb:.1f} MB).\n\n"
            "<i>This does NOT affect cached songs in Telegram or MongoDB.\n"
            "Only clean when no downloads are running.</i>"
        )
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                text, reply_markup=confirm_keyboard("clean_downloads"), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed in clean_downloads: %s", e)

    @app.on_callback_query(filters.regex("^clean_downloads_confirm$"))
    async def clean_downloads_confirm_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.utils.server_utils import clean_all_downloads, _human_size
        result = await clean_all_downloads()
        from tunedrop.app.handlers.admin import build_storage_text, storage_keyboard
        text = await build_storage_text()
        text += f"\n\n✅ <b>Cleaned:</b> {result['cleaned']} files freed ({_human_size(result['freed'])})"
        await callback_query.answer("Downloads cleaned!")
        try:
            await callback_query.message.edit_text(
                text, reply_markup=storage_keyboard(), parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed after clean_downloads_confirm: %s", e)

    # ── Admin: Active Tasks ──
    @app.on_callback_query(filters.regex("^active_tasks$"))
    async def active_tasks_callback(_, callback_query):
        admin_id = callback_query.from_user.id
        if admin_id not in settings.admin_user_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return
        from tunedrop.app.handlers.admin import build_active_tasks_text
        text = await build_active_tasks_text()
        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="back_admin")]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.warning("edit_text failed in active_tasks: %s", e)


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

    # ── Stars payment: success (raw update) ──
    @app.on_raw_update(group=10)
    async def on_successful_payment_raw(client: Client, update, users, chats):
        # Handle successful payment from raw update
        if not isinstance(update, UpdateNewMessage):
            return

        message = update.message
        if not isinstance(message, RawMessage):
            return

        # Check if message has successful payment media
        if not hasattr(message, 'media') or not message.media:
            return

        # Check if it's a successful payment by looking for the 'payload' attribute
        media = message.media
        if not hasattr(media, 'payload'):
            return
        
        # This is a successful payment
        payment = media
        logger.info("=== RAW: Successful payment received ===")

        # Get the invoice payload from successful payment
        invoice_payload = getattr(payment, 'payload', None)
        total_amount = getattr(payment, 'total_amount', 0)

        logger.info("Invoice payload: %s", invoice_payload)
        logger.info("Total amount: %s", total_amount)

        if not invoice_payload or not str(invoice_payload).startswith("tunedrop_donate:"):
            logger.warning("Payment payload doesn't match our format: %s", invoice_payload)
            return

        parts = str(invoice_payload).split(":")
        if len(parts) < 3:
            logger.warning("Invalid donation payload: %s", invoice_payload)
            return

        try:
            paid_user_id = int(parts[1])
            stars_amount = int(parts[2])
        except (ValueError, IndexError):
            logger.warning("Failed to parse donation payload: %s", invoice_payload)
            return

        # Use the amount from payment if available
        final_amount = total_amount or stars_amount

        await subscription_service.record_donation(paid_user_id, final_amount)
        logger.info("Recorded donation: user %d, %d Stars", paid_user_id, final_amount)

        # Get user ID from message
        user_id = None
        if hasattr(message, 'from_id') and hasattr(message.from_id, 'user_id'):
            user_id = message.from_id.user_id
        elif hasattr(message, 'peer_id') and hasattr(message.peer_id, 'user_id'):
            user_id = message.peer_id.user_id

        if not user_id:
            logger.warning("Could not extract user ID from payment message")
            return

        # Grant Pro for qualifying payments
        pro_activated = False
        pro_extended = False
        if final_amount >= _PRO_PRICE:
            was_pro = await subscription_service.is_pro(user_id)
            pro_until = await subscription_service.grant_pro(user_id, days=_PRO_DAYS)
            if was_pro:
                pro_extended = True
            else:
                pro_activated = True
            logger.info("Pro %s for user %d until %s", "extended" if pro_extended else "activated", user_id, pro_until.isoformat())

        # Send thank you message
        try:
            if pro_activated:
                from tunedrop.app.utils.ui_utils import format_expiry
                text = (
                    f"<b>🎉 Welcome to TuneDrop Pro!</b>\n\n"
                    f"Your benefits are now active:\n"
                    f"📥 Instant delivery in Telegram\n"
                    f"🚫 No ads on download pages\n"
                    f"⚡ Priority download queue\n\n"
                    f"⭐ <b>{format_expiry(pro_until)}</b>\n\n"
                    f"Thank you for supporting TuneDrop! ❤️\n\n"
                    f"<i>Want to be mentioned in our channel?</i>"
                )
            elif pro_extended:
                from tunedrop.app.utils.ui_utils import format_expiry
                text = (
                    f"<b>🎉 Pro Extended!</b>\n\n"
                    f"+{_PRO_DAYS} days added.\n"
                    f"⭐ <b>{format_expiry(pro_until)}</b>\n\n"
                    f"Thank you for your continued support! ❤️\n\n"
                    f"<i>Want to be mentioned in our channel?</i>"
                )
            else:
                text = (
                    f"<b>❤️ Thank you for your support!</b>\n\n"
                    f"You donated <b>{final_amount}</b> ⭐\n"
                    f"Your generosity helps keep TuneDrop free for everyone.\n\n"
                    f"<i>Want Pro? /donation</i>\n\n"
                    f"<i>Want to be mentioned in our channel?</i>"
                )
            await client.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❤️ Show my name", callback_data=f"donor_public:{final_amount}")],
                    [InlineKeyboardButton("🙈 Stay anonymous", callback_data=f"donor_anon:{final_amount}")],
                ]),
            )
            logger.info("Sent thank you message to user %d", user_id)
        except Exception as e:
            logger.error("Failed to send thank you message: %s", e, exc_info=True)

    # ── Callback: donate button in channel ──
    @app.on_callback_query(filters.regex("^donate_trigger$"))
    async def donate_trigger_callback(client: Client, callback_query):
        from tunedrop.app.handlers.start import _build_donation_text_for_user, _build_donation_keyboard
        user_id = callback_query.from_user.id
        # Send donation page to user in DM
        try:
            text = await _build_donation_text_for_user(user_id)
            await client.send_message(
                user_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_build_donation_keyboard(),
            )
            await callback_query.answer()
        except Exception:
            logger.warning("Failed to send donation page from channel click", exc_info=True)
            await callback_query.answer("Check your DM!")
