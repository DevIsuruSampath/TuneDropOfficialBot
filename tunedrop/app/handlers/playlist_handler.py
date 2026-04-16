from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.services.link_generator import link_store
from tunedrop.app.utils.decorators import once_per_message
from tunedrop.app.utils.ui_utils import escape_html

_PAGE_SIZE = 5


def register(app: Client) -> None:

    async def _send_files_list(target, files, page):
        """Send or edit the file list page. target = Message or CallbackQuery."""
        start = page * _PAGE_SIZE
        end = start + _PAGE_SIZE
        page_files = files[start:end]
        total = max(1, (len(files) + _PAGE_SIZE - 1) // _PAGE_SIZE)

        buttons = []
        for item in page_files:
            token = item.get("token", "")
            name = item.get("name", "file")[:30]
            buttons.append([InlineKeyboardButton(name, callback_data=f"file:{token}")])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"files_page:{page - 1}"))
        if page < total - 1:
            nav.append(InlineKeyboardButton("Next ➡", callback_data=f"files_page:{page + 1}"))
        nav.append(InlineKeyboardButton("✖ Close", callback_data="close_myfiles"))
        buttons.append(nav)

        text = (
            f"<b>📂 Your Downloads</b>\n"
            f"<i>Tap a file for options.</i>\n\n"
            f"📄 Page {page + 1}/{total}"
        )
        markup = InlineKeyboardMarkup(buttons)

        if hasattr(target, "message"):
            # CallbackQuery
            try:
                await target.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
            await target.answer()
        else:
            # Message
            await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    @app.on_message(filters.command("myfiles"))
    @once_per_message
    async def myfiles_handler(_, message):
        user = message.from_user
        if not user:
            return
        files = await link_store.list_user_files(user.id)
        if not files:
            await message.reply_text(
                "<b>📂 No active downloads</b>\n\n"
                "Links expire after 24 hours.\n"
                "Send a song or playlist link to get started!",
                parse_mode=ParseMode.HTML,
            )
            return
        await _send_files_list(message, files, 0)

    # ── Pagination ──
    @app.on_callback_query(filters.regex(r"^files_page:(\d+)$"))
    async def files_page_callback(_, callback_query):
        user = callback_query.from_user
        if not user:
            return
        page = int(callback_query.data.split(":")[1])
        files = await link_store.list_user_files(user.id)
        if not files:
            await callback_query.message.edit_text("<b>No active downloads</b>", parse_mode=ParseMode.HTML)
            return
        await _send_files_list(callback_query, files, page)

    # ── File detail ──
    @app.on_callback_query(filters.regex(r"^file:(.+)$"))
    async def file_detail_callback(_, callback_query):
        user = callback_query.from_user
        if not user:
            return
        token = callback_query.data.split(":", 1)[1]
        files = await link_store.list_user_files(user.id)
        found = next((f for f in files if f.get("token") == token), None)
        if not found:
            await callback_query.answer("Not found.", show_alert=True)
            return

        name = escape_html(found.get("name", "file"))
        size = found.get("size_text", "?")
        link = found.get("link", "")

        buttons = [
            [InlineKeyboardButton("⬇️ Download", url=link)],
            [InlineKeyboardButton("🗑 Revoke", callback_data=f"revoke_ask:{token}")],
            [
                InlineKeyboardButton("📁 Files", callback_data="files_page:0"),
                InlineKeyboardButton("✖ Close", callback_data="close_myfiles"),
            ],
        ]

        text = f"<b>📥 {name}</b>\n\n💾 <code>{size}</code>"
        try:
            await callback_query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass

    # ── Revoke confirm ──
    @app.on_callback_query(filters.regex(r"^revoke_ask:(.+)$"))
    async def revoke_ask_callback(_, callback_query):
        token = callback_query.data.split(":", 1)[1]
        await callback_query.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes", callback_data=f"revoke_yes:{token}"),
                InlineKeyboardButton("❌ No", callback_data=f"file:{token}"),
            ]]),
        )
        await callback_query.answer("Revoke this link?")

    # ── Revoke confirmed ──
    @app.on_callback_query(filters.regex(r"^revoke_yes:(.+)$"))
    async def revoke_yes_callback(_, callback_query):
        user = callback_query.from_user
        if not user:
            return
        token = callback_query.data.split(":", 1)[1]
        await link_store.revoke_file(user.id, token)
        await callback_query.answer("🗑 Revoked!")

        files = await link_store.list_user_files(user.id)
        if not files:
            await callback_query.message.edit_text(
                "<b>No active downloads</b>\n\n"
                "Links expire after 24 hours.\n"
                "Send a song or playlist to get started!",
                parse_mode=ParseMode.HTML,
            )
            return
        await _send_files_list(callback_query, files, 0)

    # ── Close ──
    @app.on_callback_query(filters.regex(r"^close_myfiles$"))
    async def close_myfiles_callback(_, callback_query):
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        await callback_query.answer()
