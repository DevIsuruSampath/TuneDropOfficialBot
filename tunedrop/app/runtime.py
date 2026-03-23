from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
from enum import StrEnum
from typing import Final

from tunedrop.app.core.database import close_database, init_database
from tunedrop.app.core.client import create_bot_client, register_bot_commands, register_handlers
from tunedrop.app.core.config import RuntimeTarget, settings
from tunedrop.app.core.logging import setup_logging


class RunMode(StrEnum):
    BOT = "bot"
    WEB = "web"
    ALL = "all"


DEFAULT_MODE: Final[RunMode] = RunMode.ALL


def configure_runtime(mode: RunMode) -> None:
    settings.ensure_directories()
    settings.validate(RuntimeTarget(mode.value))
    setup_logging()


def _acquire_pid_lock() -> int:
    """Acquire an exclusive file lock to prevent multiple bot instances.

    Returns the file descriptor that holds the lock.  The lock is
    automatically released when the descriptor is closed (on process exit).
    """
    lock_path = settings.data_dir / "bot.pid"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        try:
            existing = lock_path.read_text().strip()
        except OSError:
            existing = "?"
        raise RuntimeError(
            f"Another bot instance is already running (PID {existing}). "
            f"Stop it before starting a new one."
        )
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


async def run_bot() -> None:
    pid_fd = _acquire_pid_lock()
    try:
        bot = create_bot_client()
        register_handlers(bot)
        await bot.start()
        try:
            await register_bot_commands(bot)
            await asyncio.Event().wait()
        finally:
            await bot.stop()
    finally:
        fcntl.flock(pid_fd, fcntl.LOCK_UN)
        os.close(pid_fd)


async def run_web_server() -> None:
    import uvicorn

    from tunedrop.app.web.server import create_web_app

    web_app = create_web_app()
    config = uvicorn.Config(
        web_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


def _coerce_mode(mode: str | RunMode) -> RunMode:
    if isinstance(mode, RunMode):
        return mode
    return RunMode(mode)


async def run_mode(mode: str | RunMode) -> None:
    resolved_mode = _coerce_mode(mode)
    configure_runtime(resolved_mode)
    await init_database()
    try:
        if resolved_mode is RunMode.BOT:
            await run_bot()
            return
        if resolved_mode is RunMode.WEB:
            await run_web_server()
            return

        bot_task = asyncio.create_task(run_bot())
        web_task = asyncio.create_task(run_web_server())
        done, pending = await asyncio.wait(
            [bot_task, web_task], return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await close_database()


def run(mode: str | RunMode) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_mode(mode))
