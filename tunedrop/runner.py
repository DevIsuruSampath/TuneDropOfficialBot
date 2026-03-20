from __future__ import annotations

import asyncio
import contextlib


async def run_bot() -> None:
    from bot.client import create_bot_client, register_bot_commands, register_handlers

    app = create_bot_client()
    register_handlers(app)
    await app.start()
    try:
        await register_bot_commands(app)
        await asyncio.Event().wait()
    finally:
        await app.stop()


async def run_web() -> None:
    import uvicorn

    from config import settings
    from web.server import create_web_app

    web_app = create_web_app()
    config = uvicorn.Config(
        web_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


def prepare_runtime() -> None:
    from bot.utils.logger import setup_logging
    from config import settings

    settings.ensure_directories()
    settings.validate()
    setup_logging()


async def run_all() -> None:
    prepare_runtime()
    await asyncio.gather(run_bot(), run_web())


async def run_bot_only() -> None:
    prepare_runtime()
    await run_bot()


async def run_web_only() -> None:
    prepare_runtime()
    await run_web()


def run_with_signal_handling(coro: object) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(coro)
