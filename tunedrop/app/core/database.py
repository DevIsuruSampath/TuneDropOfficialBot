from __future__ import annotations

import asyncio
import threading

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from tunedrop.app.core.config import settings


_client: AsyncMongoClient | None = None
_database = None
_init_lock: asyncio.Lock | None = None
_thread_lock = threading.Lock()


def _get_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        with _thread_lock:
            if _init_lock is None:
                _init_lock = asyncio.Lock()
    return _init_lock


async def init_database():
    global _client, _database

    if _database is not None:
        return _database

    async with _get_lock():
        if _database is not None:
            return _database

        client = AsyncMongoClient(settings.mongodb_uri)
        database = client[settings.mongodb_database]
        await database.command({"ping": 1})

        await database["file_links"].create_index([("token", ASCENDING)], unique=True)
        await database["file_links"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await database["file_links"].create_index("created_at", expireAfterSeconds=86400)
        await database["user_files"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await database["user_files"].create_index("created_at", expireAfterSeconds=86400)
        await database["active_tasks"].create_index([("user_id", ASCENDING)], unique=True)
        await database["active_tasks"].create_index("created_at", expireAfterSeconds=86400)
        await database["cached_songs"].create_index([("cache_key", ASCENDING)], unique=True)

        _client = client
        _database = database
        return _database


def get_database():
    if _database is None:
        raise RuntimeError("MongoDB has not been initialized.")
    return _database


async def close_database() -> None:
    global _client, _database

    if _client is None:
        return

    await _client.close()
    _client = None
    _database = None
