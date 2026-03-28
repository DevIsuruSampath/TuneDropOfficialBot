from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.errors import QueryIdInvalid

from tunedrop.app.services.progress import task_registry

logger = logging.getLogger(__name__)


async def _safe_answer(callback_query, text: str, *, show_alert: bool = False) -> None:
    try:
        await callback_query.answer(text, show_alert=show_alert)
    except QueryIdInvalid:
        logger.debug("Callback query %s expired before answer", callback_query.id)


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^cancel:"))
    async def cancel_callback(client: Client, callback_query):
        user = callback_query.from_user
        if not user:
            await _safe_answer(callback_query, "Not available.", show_alert=True)
            return

        task_id = callback_query.data.split(":", 1)[1]
        if not task_id:
            await _safe_answer(callback_query, "Invalid task.", show_alert=True)
            return

        task = task_registry._tasks.get(task_id)
        if not task or task.user_id != user.id:
            await _safe_answer(callback_query, "Task not found.", show_alert=True)
            return

        cancelled = await task_registry.cancel(task_id)
        if cancelled:
            await _safe_answer(callback_query, "Cancelled!")
            msg = callback_query.message
            if msg:
                try:
                    await msg.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.debug("Failed to clear reply_markup on cancel", exc_info=True)
        else:
            await _safe_answer(callback_query, "No active task.", show_alert=True)

    @app.on_callback_query(filters.regex("^retry$"))
    async def retry_callback(client: Client, callback_query):
        user = callback_query.from_user
        if not user:
            await _safe_answer(callback_query, "Not available.", show_alert=True)
            return

        msg = callback_query.message
        if not msg:
            await _safe_answer(callback_query, "Message unavailable.", show_alert=True)
            return

        retried = await task_registry.retry_download(client, msg, user.id)
        if not retried:
            await _safe_answer(callback_query, "Nothing to retry.", show_alert=True)
        else:
            await _safe_answer(callback_query, "Retrying!")
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.debug("Failed to clear retry markup", exc_info=True)
