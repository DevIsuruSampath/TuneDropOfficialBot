from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import shutil

logger = logging.getLogger(__name__)

from tunedrop.app.core.database import close_database, init_database
from tunedrop.app.core.client import create_bot_client, register_bot_commands, register_handlers
from tunedrop.app.core.config import settings
from tunedrop.app.core.logging import setup_logging


def configure_runtime() -> None:
    settings.ensure_directories()
    settings.validate()
    setup_logging()


def _cleanup_temp_dirs() -> None:
    """Remove orphaned temp directories, ZIPs, and playlists from previous runs."""
    for base_dir in (settings.temp_dir, settings.zip_dir, settings.playlists_dir):
        if not base_dir.exists():
            continue
        removed = 0
        for entry in base_dir.iterdir():
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
        if removed:
            logger.info("Cleaned up %d orphaned items from %s", removed, base_dir.name)


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


async def run() -> None:
    configure_runtime()
    await init_database()
    _cleanup_temp_dirs()
    try:
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
        from tunedrop.app.utils.ffmpeg_utils import close_shared_client
        await close_shared_client()


def start() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
