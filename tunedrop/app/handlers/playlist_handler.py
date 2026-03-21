from __future__ import annotations

from pyrogram import Client, filters

from tunedrop.app.services.link_generator import link_store
from tunedrop.app.utils.decorators import once_per_message
from tunedrop.app.utils.ui_utils import escape_html


def register(app: Client) -> None:
    @app.on_message(filters.command("myfiles"))
    @once_per_message
    async def myfiles_handler(_, message):
        user = message.from_user
        if not user:
            await message.reply_text("<b>❓ User not found.</b>", parse_mode="HTML")
            return

        files = await link_store.list_user_files(user.id)
        if not files:
            await message.reply_text("<b>📁 No stored playlist files yet.</b>", parse_mode="HTML")
            return

        lines = ["<b>📁 Your recent playlist files:</b>"]
        for item in files:
            name = escape_html(item["name"])
            size = escape_html(item["size_text"])
            link = escape_html(item["link"])
            lines.append(f"📄 <code>{name}</code> • <code>{size}</code>\n🔗 <code>{link}</code>")
        await message.reply_text("\n\n".join(lines), disable_web_page_preview=True, parse_mode="HTML")
