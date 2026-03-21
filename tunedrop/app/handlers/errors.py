from __future__ import annotations

import logging

from pyrogram import Client, filters

from tunedrop.app.services.progress import task_registry


logger = logging.getLogger(__name__)


def register(app: Client) -> None:
    @app.on_message(filters.command("cancel"))
    async def cancel_handler(_, message):
        user = message.from_user
        if not user:
            await message.reply_text("\u2753 User not found.")
            return
        cancelled = await task_registry.cancel(user.id)
        await message.reply_text(
            "\u274c Task cancelled." if cancelled else "\u2753 No active task to cancel."
        )
