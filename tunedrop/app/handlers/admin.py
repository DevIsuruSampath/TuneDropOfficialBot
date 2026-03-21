from __future__ import annotations

from pyrogram import Client, filters
from pyrogram import StopPropagation

from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.decorators import admin_only
from tunedrop.app.utils.filters import fresh


def register(app: Client) -> None:
    @app.on_message(fresh & filters.command("stats"))
    @admin_only
    async def stats_handler(_, message):
        active = task_registry.active_count
        await message.reply_text(f"Active tasks: {active}")
        raise StopPropagation
