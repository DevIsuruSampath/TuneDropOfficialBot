"""Simple in-memory TTL cache for hot data optimization.

Avoids repeated MongoDB/Telegram API round-trips for recently-accessed data.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Registry of all MemoryCache instances for bulk cleanup
_all_caches: list[MemoryCache] = []


class MemoryCache:
    def __init__(self, max_size: int = 1024, ttl: float = 300.0):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._max_size = max_size
        self._ttl = ttl
        _all_caches.append(self)

    def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._cache[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: float = 0.0) -> None:
        if ttl <= 0:
            ttl = self._ttl
        if len(self._cache) >= self._max_size:
            self.evict_expired()
            if len(self._cache) >= self._max_size:
                # Evict oldest entry
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
        self._cache[key] = (value, time.monotonic() + ttl)

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def evict_expired(self) -> int:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._cache.items() if now > exp]
        for k in expired:
            del self._cache[k]
        return len(expired)

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


async def start_cleanup_task(interval: float = 1800.0) -> None:
    """Background task that evicts expired entries from all caches every 30 min."""
    while True:
        await asyncio.sleep(interval)
        total = 0
        for cache in _all_caches:
            total += cache.evict_expired()
        if total:
            logger.debug("Cache cleanup: evicted %d expired entries across %d caches", total, len(_all_caches))


# Module-level singletons
_song_cache: MemoryCache = MemoryCache(max_size=10000)
_file_url_cache: MemoryCache = MemoryCache(max_size=500, ttl=3600.0)
