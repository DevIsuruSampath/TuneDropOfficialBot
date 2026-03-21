from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.errors import StopPropagation

from tunedrop.app.services.link_generator import link_store
from tunedrop.app.utils.filters import fresh


def register(app: Client) -> None:
    @app.on_message(fresh & filters.command("myfiles"))
    async def myfiles_handler(_, message):
        user = message.from_user
        if not user:
            await message.reply_text("\u2753 User not found.")
            return

        files = await link_store.list_user_files(user.id)
        if not files:
            await message.reply_text("\U0001f4c2 No stored playlist files yet.")
            return

        lines = ["\U0001f4c2 Your recent playlist files:"]
        for item in files:
            lines.append(
                f"\U0001f4c4 {item['name']} \u2022 {item['size_text']}\n\U0001f517 {item['link']}"
            )
        await message.reply_text("\n\n".join(lines), disable_web_page_preview=True)
        raise StopPropagation
