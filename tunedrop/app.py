from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any

from tunedrop.bot import run_bot
from tunedrop.web import run_web


def prepare_runtime() -> None:
    from app.core.config import settings
    from app.core.logging import setup_logging

    settings.ensure_directories()
    settings.validate()
    setup_logging()


async def run_all() -> None:
    prepare_runtime()
    await asyncio.gather(run_bot(), run_web())


def run_with_signal_handling(coro: Coroutine[Any, Any, Any]) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(coro)
