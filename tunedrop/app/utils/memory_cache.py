"""LRU in-memory TTL cache for hot data optimization.

Avoids repeated MongoDB/Telegram API round-trips for recently-accessed data.
Uses OrderedDict for true O(1) LRU eviction (not FIFO).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# Registry of all MemoryCache instances for bulk cleanup
_all_caches: list[MemoryCache] = []


class MemoryCache:
    def __init__(self, max_size: int = 1024, ttl: float = 300.0):
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
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
        # Move to end — most recently used (LRU)
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: float = 0.0) -> None:
        if ttl <= 0:
            ttl = self._ttl
        if key in self._cache:
            # Update existing — move to end
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._max_size:
                self._evict_expired()
                if len(self._cache) >= self._max_size:
                    # Evict least recently used (oldest) entry — O(1)
                    self._cache.popitem(last=False)
        self._cache[key] = (value, time.monotonic() + ttl)

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def _evict_expired(self) -> int:
        now = time.monotonic()
        expired = 0
        # Expired entries are clustered at the front (oldest) — stop early
        keys_to_delete = []
        for k, (_, exp) in self._cache.items():
            if now > exp:
                keys_to_delete.append(k)
            else:
                break  # remaining entries are newer, no need to check
        # Also scan from the end for entries inserted with short TTL
        for k, (_, exp) in reversed(self._cache.items()):
            if now > exp and k not in keys_to_delete:
                keys_to_delete.append(k)
            else:
                break
        for k in keys_to_delete:
            self._cache.pop(k, None)
            expired += 1
        return expired

    def evict_expired(self) -> int:
        return self._evict_expired()

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


# Module-level singletons — sized for high-traffic
_song_cache: MemoryCache = MemoryCache(max_size=20000)
_file_url_cache: MemoryCache = MemoryCache(max_size=1000, ttl=3600.0)
