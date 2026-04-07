"""Performance timing utilities for tracking download pipeline stage durations."""
from __future__ import annotations

import functools
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("perf")


def timed(name: str):
    """Decorator to track async function execution time.

    Logs to perf.{name} in milliseconds.
    Usage: @timed("download_track")
    """
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                elapsed = time.monotonic() - start
                if elapsed > 0.5:  # Only log if >500ms
                    logger.info("PERF %s: %.0fms", name, elapsed * 1000)
                return result
            except Exception:
                elapsed = time.monotonic() - start
                logger.info("PERF %s FAILED: %.0fms", name, elapsed * 1000)
                raise
        wrapper._timed_name = name
        return wrapper
    return decorator


# ── Global counters ──

_perf_counters: dict[str, int] = {
    "cache_hits": 0,
    "cache_misses": 0,
    "file_url_hits": 0,
    "file_url_misses": 0,
}


def get_perf_stats() -> dict[str, int]:
    return dict(_perf_counters)
