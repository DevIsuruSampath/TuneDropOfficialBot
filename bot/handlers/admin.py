from __future__ import annotations

from pyrogram import Client, filters

from bot.decorators import admin_only
from bot.services.progress import task_registry


def register(app: Client) -> None:
    @app.on_message(filters.command("stats"))
    @admin_only
    async def stats_handler(_, message):
        active = task_registry.active_count
        await message.reply_text(f"Active tasks: {active}")
