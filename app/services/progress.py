from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pyrogram import Client
from pyrogram.types import Message

from app.core.config import settings
from app.utils.file_utils import read_json_file, write_json_file


logger = logging.getLogger(__name__)


DownloadCallable = Callable[[Client, Message, "DownloadTask"], Awaitable[None]]


@dataclass(slots=True)
class DownloadTask:
    user_id: int
    chat_id: int
    request: Any
    status_message: Message
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[Any] | None = None
    last_text: str = ""

    async def update(self, text: str) -> None:
        if text == self.last_text:
            return
        self.last_text = text
        with contextlib.suppress(Exception):
            await self.status_message.edit_text(text, disable_web_page_preview=True)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[int, DownloadTask] = {}

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    async def start_download(self, app: Client, message: Message, request: Any, runner: DownloadCallable) -> None:
        user_id = request.user_id
        existing = self._tasks.get(user_id)
        if existing:
            await message.reply_text("You already have a running task. Use /cancel first.")
            return

        status_message = await message.reply_text("Queued your download request...")
        task = DownloadTask(
            user_id=user_id,
            chat_id=request.chat_id,
            request=request,
            status_message=status_message,
        )
        self._tasks[user_id] = task
        self._persist()

        async def _run() -> None:
            try:
                await runner(app, message, task)
            except asyncio.CancelledError:
                await task.update("Task cancelled.")
                raise
            except Exception as exc:
                logger.exception("Download task failed for user %s", user_id)
                await task.update(f"Failed: {exc}")
            finally:
                self._tasks.pop(user_id, None)
                self._persist()

        task.worker = asyncio.create_task(_run())

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

    def _persist(self) -> None:
        payload = {
            str(user_id): {
                "chat_id": item.chat_id,
                "source": item.request.source,
                "input_type": str(item.request.input_type),
            }
            for user_id, item in self._tasks.items()
        }
        write_json_file(settings.tasks_file, payload)


task_registry = TaskRegistry()
