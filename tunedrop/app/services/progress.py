from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from pymongo import UpdateOne
from pyrogram import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from tunedrop.app.core.database import get_database
from pyrogram.enums import ParseMode

from tunedrop.app.utils.ui_utils import build_error_message, build_retry_keyboard


logger = logging.getLogger(__name__)


DownloadCallable = Callable[[Client, Message, "DownloadTask"], Awaitable[None]]

_RETRY_MARKUP = build_retry_keyboard()


def _cancel_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}")],
    ])


@dataclass(slots=True)
class DownloadTask:
    task_id: str
    user_id: int
    chat_id: int
    request: Any
    status_message: Message
    original_message_id: int = 0
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
                    logger.debug("Failed to update status message for task %s", self.task_id, exc_info=True)
        finally:
            self._updating = False

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, DownloadTask] = {}
        self._user_tasks: dict[int, set[str]] = {}
        self._queue: deque[str] = deque()
        self._pending_starts: dict[str, tuple[Client, Message, Any, DownloadCallable]] = {}
        self._failed: dict[int, tuple[Any, DownloadCallable, Client]] = {}

    @property
    def active_count(self) -> int:
        return sum(
            1 for t in self._tasks.values()
            if t.worker is not None and not t.worker.done()
        )

    def _active_task_ids_for(self, user_id: int) -> set[str]:
        user_ids = self._user_tasks.get(user_id, set())
        return {
            tid for tid in user_ids
            if tid in self._tasks and self._tasks[tid].worker is not None and not self._tasks[tid].worker.done()
        }

    def get_user_active_count(self, user_id: int) -> int:
        return len(self._active_task_ids_for(user_id))

    def has_active(self, user_id: int) -> bool:
        return len(self._active_task_ids_for(user_id)) > 0

    def pop_failed(self, user_id: int) -> tuple[Any, DownloadCallable, Client] | None:
        return self._failed.pop(user_id, None)

    async def _cleanup_user_tasks(self, user_id: int) -> None:
        user_task_ids = list(self._user_tasks.get(user_id, set()))
        for task_id in user_task_ids:
            if task_id in self._queue:
                continue
            if task_id not in self._tasks:
                self._user_tasks[user_id].discard(task_id)
                continue
            task = self._tasks[task_id]
            if task.worker is None or task.worker.done():
                self._tasks.pop(task_id, None)
                self._user_tasks[user_id].discard(task_id)
        if user_id in self._user_tasks and not self._user_tasks[user_id]:
            del self._user_tasks[user_id]

    def _generate_task_id(self) -> str:
        task_id = secrets.token_urlsafe(8)
        while task_id in self._tasks:
            task_id = secrets.token_urlsafe(8)
        return task_id

    def _is_queued(self, task_id: str) -> bool:
        return task_id in self._queue

    def _queue_position(self, task_id: str) -> int:
        for i, tid in enumerate(self._queue):
            if tid == task_id:
                return i + 1
        return 0

    async def _update_queue_positions(self) -> None:
        for i, task_id in enumerate(self._queue):
            task = self._tasks.get(task_id)
            if task and not task.cancelled():
                await task.update(
                    f"<b>Queued</b>\nPosition: #{i + 1}",
                    parse_mode=ParseMode.HTML,
                )

    async def _dequeue_next(self) -> None:
        while self._queue:
            task_id = self._queue[0]
            task = self._tasks.get(task_id)
            if not task:
                self._queue.popleft()
                self._pending_starts.pop(task_id, None)
                continue
            if task.cancelled():
                self._queue.popleft()
                self._pending_starts.pop(task_id, None)
                self._tasks.pop(task_id, None)
                if task.user_id in self._user_tasks:
                    self._user_tasks[task.user_id].discard(task_id)
                    if not self._user_tasks[task.user_id]:
                        del self._user_tasks[task.user_id]
                continue

            start_params = self._pending_starts.pop(task_id, None)
            if not start_params:
                self._queue.popleft()
                continue

            app, message, request, runner = start_params
            self._queue.popleft()
            self._failed.pop(task.user_id, None)
            task.worker = asyncio.create_task(self._run(app, message, task, request, runner))
            await self._persist()
            await self._update_queue_positions()
            break

    async def start_download(self, app: Client, message: Message, request: Any, runner: DownloadCallable) -> None:
        from tunedrop.app.core.config import settings

        user_id = request.user_id
        await self._cleanup_user_tasks(user_id)

        task_id = self._generate_task_id()
        cancel_kb = _cancel_keyboard(task_id)

        is_queued = self.active_count >= settings.max_concurrent_tasks

        status_message = await message.reply_text(
            "<b>Queued</b>" if is_queued else "<b>Starting...</b>",
            parse_mode=ParseMode.HTML,
        )

        task = DownloadTask(
            task_id=task_id,
            user_id=user_id,
            chat_id=request.chat_id,
            request=request,
            status_message=status_message,
            original_message_id=message.id,
            _reply_markup=cancel_kb,
        )
        self._tasks[task_id] = task
        if user_id not in self._user_tasks:
            self._user_tasks[user_id] = set()
        self._user_tasks[user_id].add(task_id)

        if is_queued:
            self._queue.append(task_id)
            self._pending_starts[task_id] = (app, message, request, runner)
            pos = self._queue_position(task_id)
            await task.update(
                f"<b>Queued</b>\nPosition: #{pos}",
                parse_mode=ParseMode.HTML,
            )
        else:
            self._failed.pop(user_id, None)
            task.worker = asyncio.create_task(self._run(app, message, task, request, runner))

        await self._persist()

    async def retry_download(self, client: Client, message: Message, user_id: int) -> bool:
        from tunedrop.app.core.config import settings

        await self._cleanup_user_tasks(user_id)

        failed = self._failed.pop(user_id, None)
        if not failed:
            return False

        request, runner, app = failed
        task_id = self._generate_task_id()
        cancel_kb = _cancel_keyboard(task_id)

        is_queued = self.active_count >= settings.max_concurrent_tasks

        try:
            text = "<b>Queued</b>" if is_queued else "<b>Retrying...</b>"
            await message.edit_text(text, reply_markup=cancel_kb, parse_mode=ParseMode.HTML)
        except Exception:
            return False

        task = DownloadTask(
            task_id=task_id,
            user_id=user_id,
            chat_id=message.chat.id,
            request=request,
            status_message=message,
            _reply_markup=cancel_kb,
        )
        self._tasks[task_id] = task
        if user_id not in self._user_tasks:
            self._user_tasks[user_id] = set()
        self._user_tasks[user_id].add(task_id)

        if is_queued:
            self._queue.append(task_id)
            self._pending_starts[task_id] = (app, message, request, runner)
            pos = self._queue_position(task_id)
            await task.update(
                f"<b>Queued</b>\nPosition: #{pos}",
                parse_mode=ParseMode.HTML,
            )
        else:
            task.worker = asyncio.create_task(self._run(app, message, task, request, runner))

        await self._persist()
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
            self._tasks.pop(task.task_id, None)
            if task.user_id in self._user_tasks:
                self._user_tasks[task.user_id].discard(task.task_id)
                if not self._user_tasks[task.user_id]:
                    del self._user_tasks[task.user_id]
            try:
                await self._persist()
            except RuntimeError:
                pass
            await self._dequeue_next()

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False

        was_queued = self._is_queued(task_id)

        if was_queued:
            self._queue.remove(task_id)
            self._pending_starts.pop(task_id, None)

        task.cancel_event.set()
        if task.worker:
            task.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task.worker
            # _run's finally block already cleaned up _tasks, _user_tasks,
            # and called _dequeue_next() — just update queue positions.
            await self._update_queue_positions()
            return True

        self._tasks.pop(task_id, None)
        if task.user_id in self._user_tasks:
            self._user_tasks[task.user_id].discard(task_id)
            if not self._user_tasks[task.user_id]:
                del self._user_tasks[task.user_id]

        await self._update_queue_positions()
        return True

    async def cancel_all(self, user_id: int) -> int:
        task_ids = list(self._user_tasks.get(user_id, set()))
        cancelled = 0
        for task_id in task_ids:
            if await self.cancel(task_id):
                cancelled += 1
        return cancelled

    async def _persist(self) -> None:
        db = get_database()
        collection = db["active_tasks"]
        now = datetime.now(UTC)
        active_task_ids = list(self._tasks.keys())

        operations = [
            UpdateOne(
                {"task_id": task_id},
                {"$set": {
                    "task_id": task_id,
                    "user_id": item.user_id,
                    "chat_id": item.chat_id,
                    "source": item.request.source,
                    "input_type": str(item.request.input_type),
                    "created_at": now,
                }},
                upsert=True,
            )
            for task_id, item in self._tasks.items()
        ]

        if operations:
            await collection.bulk_write(operations)

        if active_task_ids:
            await collection.delete_many({"task_id": {"$nin": active_task_ids}})
        else:
            await collection.delete_many({})


task_registry = TaskRegistry()
