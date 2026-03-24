from __future__ import annotations

from pyrogram import Client, filters

from tunedrop.app.services.progress import task_registry
from tunedrop.app.utils.decorators import admin_only, once_per_message


def register(app: Client) -> None:
    @app.on_message(filters.command("stats"))
    @admin_only
    @once_per_message
    async def stats_handler(_, message):
        active = task_registry.active_count
        users = len(task_registry._user_tasks)
        await message.reply_text(f"Active tasks: {active}\nUsers: {users}")
