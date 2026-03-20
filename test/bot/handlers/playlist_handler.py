from __future__ import annotations

from pyrogram import Client, filters

from bot.services.link_generator import link_store


def register(app: Client) -> None:
    @app.on_message(filters.command("myfiles"))
    async def myfiles_handler(_, message):
        user = message.from_user
        if not user:
            await message.reply_text("User not found.")
            return

        files = await link_store.list_user_files(user.id)
        if not files:
            await message.reply_text("No stored playlist files yet.")
            return

        lines = ["Your recent playlist files:"]
        for item in files[:10]:
            lines.append(
                f"- {item['name']} | {item['size_text']} | {item['link']}"
            )
        await message.reply_text("\n".join(lines), disable_web_page_preview=True)
