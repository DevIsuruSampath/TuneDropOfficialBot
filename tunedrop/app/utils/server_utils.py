from __future__ import annotations

import asyncio
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from tunedrop.app.core.config import settings

_start_time = time.monotonic()


def get_uptime() -> str:
    """Get bot process uptime as human-readable string."""
    elapsed = time.monotonic() - _start_time
    days = int(elapsed // 86400)
    hours = int((elapsed % 86400) // 3600)
    minutes = int((elapsed % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_system_stats() -> dict[str, Any]:
    """Get system resource usage (CPU, RAM, disk)."""
    stats: dict[str, Any] = {}

    try:
        import psutil
        stats["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        stats["cpu_count"] = psutil.cpu_count() or os.cpu_count() or 0
        mem = psutil.virtual_memory()
        stats["ram_total_gb"] = round(mem.total / (1024**3), 1)
        stats["ram_used_gb"] = round(mem.used / (1024**3), 1)
        stats["ram_percent"] = mem.percent
        stats["ram_available_gb"] = round(mem.available / (1024**3), 1)
    except ImportError:
        try:
            load = os.getloadavg()
            stats["load_1m"] = round(load[0], 2)
            stats["load_5m"] = round(load[1], 2)
        except Exception:
            pass

    # Disk usage (always available via shutil)
    try:
        disk = shutil.disk_usage(str(settings.base_dir))
        stats["disk_total_gb"] = round(disk.total / (1024**3), 1)
        stats["disk_used_gb"] = round(disk.used / (1024**3), 1)
        stats["disk_free_gb"] = round(disk.free / (1024**3), 1)
        stats["disk_percent"] = round(disk.used / disk.total * 100, 1)
    except Exception:
        pass

    # Process info
    try:
        import psutil
        proc = psutil.Process()
        stats["proc_mem_mb"] = round(proc.memory_info().rss / (1024**2), 1)
        stats["proc_cpu"] = proc.cpu_percent(interval=0.1)
        stats["threads"] = proc.num_threads()
    except Exception:
        pass

    return stats


def get_storage_breakdown() -> dict[str, dict[str, Any]]:
    """Get size breakdown of each data directory."""
    dirs: list[tuple[str, Path]] = [
        ("temp", settings.temp_dir),
        ("songs", settings.songs_dir),
        ("playlists", settings.playlists_dir),
        ("zip", settings.zip_dir),
        ("logs", settings.logs_dir),
        ("data", settings.data_dir),
    ]
    result: dict[str, dict[str, Any]] = {}
    for name, path in dirs:
        if not path.exists():
            result[name] = {"size": 0, "files": 0, "human": "0 B"}
            continue
        total_size = 0
        total_files = 0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    try:
                        total_size += f.stat().st_size
                        total_files += 1
                    except OSError:
                        pass
        except Exception:
            pass
        result[name] = {
            "size": total_size,
            "files": total_files,
            "human": _human_size(total_size),
        }
    return result


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


async def clean_temp_files() -> dict[str, int]:
    """Clean temp, playlists, and zip directories. Returns {cleaned, freed}."""
    cleaned = 0
    freed = 0
    for base_dir in (settings.temp_dir, settings.zip_dir, settings.playlists_dir):
        if not base_dir.exists():
            continue
        for entry in list(base_dir.iterdir()):
            try:
                if entry.is_dir():
                    for f in entry.rglob("*"):
                        if f.is_file():
                            try:
                                freed += f.stat().st_size
                            except OSError:
                                pass
                            cleaned += 1
                    shutil.rmtree(entry, ignore_errors=True)
                elif entry.is_file():
                    try:
                        freed += entry.stat().st_size
                    except OSError:
                        pass
                    cleaned += 1
                    entry.unlink(missing_ok=True)
            except Exception:
                pass
    return {"cleaned": cleaned, "freed": freed}


async def clean_logs() -> dict[str, int]:
    """Clean log files. Returns {cleaned, freed}."""
    cleaned = 0
    freed = 0
    if not settings.logs_dir.exists():
        return {"cleaned": 0, "freed": 0}
    for f in settings.logs_dir.iterdir():
        if f.is_file() and f.suffix in (".log", ".log.old", ".txt"):
            try:
                freed += f.stat().st_size
                f.unlink()
                cleaned += 1
            except Exception:
                pass
    return {"cleaned": cleaned, "freed": freed}


async def clean_all_downloads() -> dict[str, int]:
    """Clean all downloads directories (songs + temp + playlists + zip). Returns {cleaned, freed}."""
    cleaned = 0
    freed = 0
    for base_dir in (settings.temp_dir, settings.zip_dir, settings.playlists_dir, settings.songs_dir):
        if not base_dir.exists():
            continue
        for entry in list(base_dir.iterdir()):
            try:
                if entry.is_dir():
                    for f in entry.rglob("*"):
                        if f.is_file():
                            try:
                                freed += f.stat().st_size
                            except OSError:
                                pass
                            cleaned += 1
                    shutil.rmtree(entry, ignore_errors=True)
                elif entry.is_file():
                    try:
                        freed += entry.stat().st_size
                    except OSError:
                        pass
                    cleaned += 1
                    entry.unlink(missing_ok=True)
            except Exception:
                pass
    return {"cleaned": cleaned, "freed": freed}
