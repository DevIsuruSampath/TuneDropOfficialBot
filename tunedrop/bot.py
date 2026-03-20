from __future__ import annotations

import asyncio


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
