from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fcntl
import logging
import os
import shutil

logger = logging.getLogger(__name__)

from tunedrop.app.core.database import close_database, init_database
from tunedrop.app.core.client import (
    close_aiogram_bot,
    create_bot_client,
    create_bot_client_with_token,
    get_all_clients,
    register_bot_commands,
    register_handlers,
    register_primary_only_handlers,
    set_all_clients,
    set_pyrogram_client,
)
from tunedrop.app.core.config import settings
from tunedrop.app.core.logging import setup_logging
from tunedrop.app.utils.ffmpeg_utils import close_shared_client
from tunedrop.app.web.server import create_web_app

# Dedicated thread pool for CPU-bound / blocking I/O (yt-dlp, ffprobe, ZIP, etc.)
# Sized via THREAD_POOL_WORKERS env var — default 8 is tuned for 2-CPU servers.
_thread_pool: concurrent.futures.ThreadPoolExecutor | None = None


def configure_runtime() -> None:
    settings.ensure_directories()
    settings.validate()
    setup_logging()


def _cleanup_temp_dirs() -> None:
    """Remove orphaned temp directories, ZIPs, and playlists from previous runs."""
    total_removed = 0
    total_freed = 0
    for base_dir in (settings.temp_dir, settings.zip_dir, settings.playlists_dir):
        if not base_dir.exists():
            continue
        removed = 0
        freed = 0
        for entry in base_dir.iterdir():
            try:
                if entry.is_dir():
                    for f in entry.rglob("*"):
                        if f.is_file():
                            try:
                                freed += f.stat().st_size
                            except OSError:
                                pass
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    try:
                        freed += entry.stat().st_size
                    except OSError:
                        pass
                    entry.unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
        total_removed += removed
        total_freed += freed
        if removed:
            logger.info("Cleaned up %d orphaned items from %s (%.1f MB freed)", removed, base_dir.name, freed / (1024**2))
    if total_removed:
        logger.info("Startup cleanup: %d items removed, %.1f MB freed", total_removed, total_freed / (1024**2))


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
    clients: list = []
    try:
        # 1. Create primary client
        primary = create_bot_client()
        register_handlers(primary)
        register_primary_only_handlers(primary)
        set_pyrogram_client(primary)
        clients.append(primary)

        # 2. Create secondary clients from MULTI_TOKEN* env vars
        for i, token in enumerate(settings.multi_tokens, start=1):
            client = create_bot_client_with_token(token, session_suffix=f"_{i}")
            register_handlers(client)
            clients.append(client)
            logger.info("Secondary bot client #%d configured", i)

        set_all_clients(clients)
        if len(clients) > 1:
            logger.info("Multi-client mode: %d bots (1 primary + %d secondary)",
                        len(clients), len(clients) - 1)

        # 3. Start all clients sequentially (avoids DC auth rate limits)
        for c in clients:
            await c.start()

        # 4. Register bot commands on all clients
        for c in clients:
            try:
                await register_bot_commands(c)
            except Exception:
                logger.warning("Failed to set bot commands for %s", c.name, exc_info=True)

        # 5. Start aiogram polling for primary bot's payments
        from tunedrop.app.core.client import get_aiogram_dispatcher, get_aiogram_bot
        from tunedrop.app.handlers.donation_aiogram import register_aiogram_handlers

        dp = get_aiogram_dispatcher()
        aiogram_bot = get_aiogram_bot()
        register_aiogram_handlers(dp)

        async def aiogram_polling():
            logger.info("Starting aiogram Bot API polling")
            await dp.start_polling(aiogram_bot, allowed_updates=["message", "callback_query"])

        asyncio.create_task(aiogram_polling())

        await asyncio.Event().wait()
    finally:
        # Stop all clients in reverse order
        for c in reversed(clients):
            try:
                await c.stop()
            except Exception:
                pass
        fcntl.flock(pid_fd, fcntl.LOCK_UN)
        os.close(pid_fd)


async def run_web_server() -> None:
    import uvicorn

    web_app = create_web_app()
    config = uvicorn.Config(
        web_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        access_log=False,  # Suppress per-request access logs (scanners flood them)
        timeout_keep_alive=30,
        # Workers handled by separate process mode if configured
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run() -> None:
    global _thread_pool
    configure_runtime()
    await init_database()
    _cleanup_temp_dirs()

    # Set up dedicated thread pool for blocking operations
    _thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=settings.thread_pool_workers,
        thread_name_prefix="tunedrop-worker",
    )
    loop = asyncio.get_running_loop()
    loop.set_default_executor(_thread_pool)
    logger.info("Thread pool initialized: %d workers", settings.thread_pool_workers)

    # Start background cache cleanup task
    from tunedrop.app.utils.memory_cache import start_cleanup_task
    asyncio.create_task(start_cleanup_task())

    # Warn about missing cookies (YouTube Music will block without them)
    if settings.ytdlp_cookie_file:
        from pathlib import Path as _P
        _cookie = _P(settings.ytdlp_cookie_file)
        if not _cookie.exists() or _cookie.stat().st_size == 0:
            logger.warning(
                "YTDLP_COOKIE_FILE=%s is empty or missing. "
                "YouTube Music downloads will be BLOCKED. "
                "Export cookies from your browser (youtube.com) and save to this file.",
                settings.ytdlp_cookie_file,
            )

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
        await close_shared_client()
        await close_aiogram_bot()
        if _thread_pool:
            _thread_pool.shutdown(wait=False)
            logger.info("Thread pool shut down")


def start() -> None:
    # Verify uvloop is active (auto-installed by pyrofork)
    try:
        import uvloop
        logger.info("uvloop %s active", uvloop.__version__)
    except ImportError:
        logger.warning("uvloop not available — falling back to default event loop")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
