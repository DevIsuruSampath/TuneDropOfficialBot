from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from pymongo import UpdateOne
from pyrogram import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from tunedrop.app.core.database import get_database
from pyrogram.enums import ParseMode

from tunedrop.app.utils.ui_utils import build_cancel_keyboard, build_error_message, build_retry_keyboard


logger = logging.getLogger(__name__)


DownloadCallable = Callable[[Client, Message, "DownloadTask"], Awaitable[None]]

_CANCEL_MARKUP = build_cancel_keyboard()
_RETRY_MARKUP = build_retry_keyboard()


@dataclass(slots=True)
class DownloadTask:
    user_id: int
    chat_id: int
    request: Any
    status_message: Message
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[Any] | None = None
    last_text: str = ""
    _reply_markup: InlineKeyboardMarkup | None = field(default=None, repr=False)
    _last_markup: InlineKeyboardMarkup | None = field(default=None, repr=False)
    _pending: tuple[str, str | None] | None = field(default=None, repr=False)
    _updating: bool = field(default=False, repr=False)

    async def update(self, text: str, parse_mode: str | None = None) -> None:
        if text == self.last_text and self._reply_markup is self._last_markup:
            return
        self._pending = (text, parse_mode)
        if self._updating:
            return
        self._updating = True
        try:
            while self._pending is not None:
                current_text, current_pm = self._pending
                self._pending = None
                self.last_text = current_text
                self._last_markup = self._reply_markup
                try:
                    await self.status_message.edit_text(
                        current_text, disable_web_page_preview=True,
                        reply_markup=self._reply_markup, parse_mode=current_pm,
                    )
                except Exception:
                    logger.debug("Failed to update status message for user %s", self.user_id, exc_info=True)
        finally:
            self._updating = False

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[int, DownloadTask] = {}
        self._failed: dict[int, tuple[Any, DownloadCallable, Client]] = {}

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def has_active(self, user_id: int) -> bool:
        task = self._tasks.get(user_id)
        return task is not None and task.worker is not None and not task.worker.done()

    def pop_failed(self, user_id: int) -> tuple[Any, DownloadCallable, Client] | None:
        return self._failed.pop(user_id, None)

    async def start_download(self, app: Client, message: Message, request: Any, runner: DownloadCallable) -> None:
        user_id = request.user_id

        # Auto-cleanup: remove task if its worker already finished
        existing = self._tasks.get(user_id)
        if existing and (existing.worker is None or existing.worker.done()):
            self._tasks.pop(user_id, None)
            existing = None

        if existing:
            await message.reply_text(
                "<b>Already downloading</b>",
                reply_markup=_CANCEL_MARKUP,
                parse_mode=ParseMode.HTML,
            )
            return

        status_message = await message.reply_text(
            "<b>Queued</b>",
            parse_mode=ParseMode.HTML,
        )
        task = DownloadTask(
            user_id=user_id,
            chat_id=request.chat_id,
            request=request,
            status_message=status_message,
            _reply_markup=_CANCEL_MARKUP,
        )
        self._tasks[user_id] = task
        self._failed.pop(user_id, None)
        await self._persist()
        task.worker = asyncio.create_task(self._run(app, message, task, request, runner))

    async def retry_download(self, client: Client, message: Message, user_id: int) -> bool:
        # Auto-cleanup stale entries
        existing = self._tasks.get(user_id)
        if existing and (existing.worker is None or existing.worker.done()):
            self._tasks.pop(user_id, None)

        failed = self._failed.pop(user_id, None)
        if not failed:
            return False

        request, runner, app = failed
        try:
            await message.edit_text("<b>Retrying...</b>", reply_markup=_CANCEL_MARKUP, parse_mode=ParseMode.HTML)
        except Exception:
            return False

        task = DownloadTask(
            user_id=user_id,
            chat_id=message.chat.id,
            request=request,
            status_message=message,
            _reply_markup=_CANCEL_MARKUP,
        )
        self._tasks[user_id] = task
        await self._persist()
        task.worker = asyncio.create_task(self._run(app, message, task, request, runner))
        return True

    async def _run(self, app: Client, message: Message, task: DownloadTask, request: Any, runner: DownloadCallable) -> None:
        try:
            await runner(app, message, task)
            task._reply_markup = None
            await task.update(task.last_text, parse_mode=ParseMode.HTML)
        except asyncio.CancelledError:
            task._reply_markup = None
            try:
                await task.update("<b>Cancelled</b>", parse_mode=ParseMode.HTML)
            except asyncio.CancelledError:
                pass
            raise
        except Exception as exc:
            logger.exception("Download task failed for user %s", task.user_id)
            self._failed[task.user_id] = (request, runner, app)
            task._reply_markup = _RETRY_MARKUP
            await task.update(build_error_message(str(exc)), parse_mode=ParseMode.HTML)
        finally:
            self._tasks.pop(task.user_id, None)
            try:
                await self._persist()
            except RuntimeError:
                pass  # Database already closed during shutdown

    async def cancel(self, user_id: int) -> bool:
        task = self._tasks.get(user_id)
        if not task:
            return False
        task.cancel_event.set()
        if task.worker:
            task.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task.worker
        return True

    async def _persist(self) -> None:
        db = get_database()
        collection = db["active_tasks"]
        now = datetime.now(UTC)
        active_user_ids = list(self._tasks.keys())

        operations = [
            UpdateOne(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "chat_id": item.chat_id,
                    "source": item.request.source,
                    "input_type": str(item.request.input_type),
                    "created_at": now,
                }},
                upsert=True,
            )
            for user_id, item in self._tasks.items()
        ]

        if operations:
            await collection.bulk_write(operations)

        if active_user_ids:
            await collection.delete_many({"user_id": {"$nin": active_user_ids}})
        else:
            await collection.delete_many({})


task_registry = TaskRegistry()
