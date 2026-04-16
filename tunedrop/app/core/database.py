from __future__ import annotations

import asyncio
import threading

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from tunedrop.app.core.config import settings


_client: AsyncMongoClient | None = None
_database: AsyncDatabase | None = None
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

        client = AsyncMongoClient(
            settings.mongodb_uri,
            maxIdleTimeMS=30000,
            minPoolSize=20,
            maxPoolSize=200,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=5000,
            waitQueueTimeoutMS=5000,
            retryWrites=True,
            retryReads=True,
        )
        database = client[settings.mongodb_database]
        await database.command({"ping": 1})

        await database["file_links"].create_index([("token", ASCENDING)], unique=True)
        await database["file_links"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await database["file_links"].create_index("created_at", expireAfterSeconds=86400)
        await database["file_links"].create_index([("file_id", ASCENDING), ("expires_at", DESCENDING)])
        await database["user_files"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await database["user_files"].create_index("created_at", expireAfterSeconds=86400)
        await database["download_refs"].create_index([("ref", ASCENDING)], unique=True)
        await database["active_tasks"].drop_indexes()
        await database["active_tasks"].create_index([("task_id", ASCENDING)], unique=True)
        await database["active_tasks"].create_index([("user_id", ASCENDING)])
        await database["active_tasks"].create_index("created_at", expireAfterSeconds=86400)
        await database["cached_songs"].create_index([("cache_key", ASCENDING)], unique=True)
        await database["users"].create_index([("user_id", ASCENDING)], unique=True)
        await database["donations"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

        _client = client
        _database = database
        return _database


def get_database() -> AsyncDatabase:
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
