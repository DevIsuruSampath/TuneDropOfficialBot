from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.decorators import once_per_message


logger = logging.getLogger(__name__)


def register(app: Client) -> None:
    @app.on_message(filters.command("cancel"))
    @once_per_message
    async def cancel_handler(_, message):
        user = message.from_user
        if not user:
            await message.reply_text("<b>❓ User not found.</b>", parse_mode=ParseMode.HTML)
            return
        cancelled = await task_registry.cancel(user.id)
        text = "<b>Cancelled</b>" if cancelled else "<b>No active task</b>"
        await message.reply_text(text, parse_mode=ParseMode.HTML)
