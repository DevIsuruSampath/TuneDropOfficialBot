from __future__ import annotations

import asyncio
import contextlib

import uvicorn

from bot.client import create_bot_client, register_handlers
from bot.services.progress import task_registry
from bot.utils.logger import setup_logging
from config import settings
from web.server import create_web_app


async def run_bot() -> None:
    app = create_bot_client()
    register_handlers(app)
    await app.start()
    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()


async def run_web() -> None:
    web_app = create_web_app()
    config = uvicorn.Config(
        web_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    settings.ensure_directories()
    setup_logging()
    await asyncio.gather(run_bot(), run_web())


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
