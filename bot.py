from __future__ import annotations

import asyncio
import contextlib

from bot.client import create_bot_client, register_bot_commands, register_handlers
from bot.utils.logger import setup_logging
from config import settings


async def main() -> None:
    settings.ensure_directories()
    settings.validate()
    setup_logging()
    app = create_bot_client()
    register_handlers(app)
    await app.start()
    try:
        await register_bot_commands(app)
        await asyncio.Event().wait()
    finally:
        await app.stop()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
