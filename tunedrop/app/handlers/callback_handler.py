from __future__ import annotations

from pyrogram import Client, filters

from tunedrop.app.services.progress import task_registry


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex("^cancel$"))
    async def cancel_callback(client: Client, callback_query):
        user = callback_query.from_user
        if not user:
            await callback_query.answer("Not available.", show_alert=True)
            return

        cancelled = await task_registry.cancel(user.id)
        if cancelled:
            await callback_query.answer("Cancelled!", show_alert=False)
            try:
                await callback_query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await callback_query.answer("No active task.", show_alert=True)

    @app.on_callback_query(filters.regex("^retry$"))
    async def retry_callback(client: Client, callback_query):
        user = callback_query.from_user
        if not user:
            await callback_query.answer("Not available.", show_alert=True)
            return

        if task_registry.has_active(user.id):
            await callback_query.answer("You have a running task.", show_alert=True)
            return

        retried = await task_registry.retry_download(client, callback_query.message, user.id)
        if not retried:
            await callback_query.answer("Nothing to retry.", show_alert=True)
        else:
            await callback_query.answer("Retrying!", show_alert=False)
