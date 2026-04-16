from __future__ import annotations

from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from tunedrop.app.core.config import settings
from tunedrop.app.core.database import get_database
from tunedrop.app.services.subscription import subscription_service


def admin_keyboard() -> InlineKeyboardMarkup:
    ads_state = "ON" if settings.ads_enabled else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("User Info", callback_data="pro_info"),
            InlineKeyboardButton("Stats", callback_data="show_stats"),
        ],
        [
            InlineKeyboardButton(f"Ads: {ads_state}", callback_data="show_ads"),
            InlineKeyboardButton("Clear Cache", callback_data="clear_cache"),
        ],
        [
            InlineKeyboardButton("Server Status", callback_data="server_status"),
            InlineKeyboardButton("Storage", callback_data="storage_menu"),
        ],
        [
            InlineKeyboardButton("Active Tasks", callback_data="active_tasks"),
            InlineKeyboardButton("Broadcast", callback_data="broadcast_start"),
        ],
        [
            InlineKeyboardButton("Rebuild Cache", callback_data="rebuild_cache"),
        ],
    ])


def ads_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ON", callback_data="ads_on"),
            InlineKeyboardButton("OFF", callback_data="ads_off"),
        ],
        [InlineKeyboardButton("Back", callback_data="back_admin")],
    ])


def storage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Clean Temp", callback_data="clean_temp"),
            InlineKeyboardButton("Clean Logs", callback_data="clean_logs"),
        ],
        [
            InlineKeyboardButton("Clean All Downloads", callback_data="clean_downloads"),
        ],
        [InlineKeyboardButton("Back", callback_data="back_admin")],
    ])


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, do it", callback_data=f"{action}_confirm"),
            InlineKeyboardButton("Cancel", callback_data="back_admin"),
        ],
    ])


async def build_admin_text() -> str:
    active = 0
    queued = 0
    ads_state = "ON" if settings.ads_enabled else "OFF"

    total_users = 0
    total_stars = 0
    pro_users = 0
    cached_songs = 0
    total_downloads = 0

    try:
        from tunedrop.app.services.progress import task_registry
        active = task_registry.active_count
        queued = task_registry.queued_count
    except Exception:
        pass

    try:
        db = get_database()
        total_users = await db["users"].count_documents({})
        total_stars = await subscription_service.get_total_donations()
        pro_users = await subscription_service.get_pro_count()
        cached_songs = await db["cached_songs"].count_documents({})
        total_downloads = await db["download_refs"].count_documents({})
    except Exception:
        pass

    bots_count = max(1, len(settings.multi_tokens) + 1) if settings.multi_tokens else 1

    return (
        "<b>Admin Panel</b>\n\n"
        f"<b>Tasks:</b> {active} active / {queued} queued\n"
        f"<b>Bots:</b> {bots_count}\n"
        f"<b>Users:</b> {total_users} total\n"
        f"⭐ <b>Pro users:</b> {pro_users}\n"
        f"⭐ <b>Stars donated:</b> {total_stars}\n"
        f"📦 <b>Cache:</b> {cached_songs} songs\n"
        f"📥 <b>Downloads:</b> {total_downloads} total\n"
        f"<b>Ads:</b> <code>{ads_state}</code>\n\n"
        "<i>Tap a button below.</i>"
    )


async def build_server_status_text() -> str:
    from tunedrop.app.utils.server_utils import get_system_stats, get_uptime
    import platform

    stats = get_system_stats()
    uptime = get_uptime()
    py_ver = f"{platform.python_version()}"
    os_name = platform.system()

    lines = ["<b>🖥 Server Status</b>\n"]

    # Uptime
    lines.append(f"⏱ <b>Uptime:</b> {uptime}")
    lines.append(f"🐍 <b>Python:</b> {py_ver}")
    lines.append(f"💻 <b>OS:</b> {os_name}")

    # CPU
    if "cpu_percent" in stats:
        cpu_emoji = "🟢" if stats["cpu_percent"] < 70 else "🟡" if stats["cpu_percent"] < 90 else "🔴"
        lines.append(f"\n{cpu_emoji} <b>CPU:</b> {stats['cpu_percent']}% ({stats.get('cpu_count', '?')} cores)")
    elif "load_1m" in stats:
        lines.append(f"\n📊 <b>Load:</b> {stats['load_1m']} / {stats['load_5m']}")

    # RAM
    if "ram_used_gb" in stats:
        ram_emoji = "🟢" if stats["ram_percent"] < 70 else "🟡" if stats["ram_percent"] < 90 else "🔴"
        lines.append(
            f"{ram_emoji} <b>RAM:</b> {stats['ram_used_gb']}/{stats['ram_total_gb']} GB "
            f"({stats['ram_percent']}%)"
        )

    # Process
    if "proc_mem_mb" in stats:
        lines.append(f"⚙️ <b>Bot process:</b> {stats['proc_mem_mb']} MB RAM, {stats.get('threads', '?')} threads")

    # Disk
    if "disk_used_gb" in stats:
        disk_emoji = "🟢" if stats["disk_percent"] < 70 else "🟡" if stats["disk_percent"] < 90 else "🔴"
        lines.append(
            f"\n{disk_emoji} <b>Disk:</b> {stats['disk_used_gb']}/{stats['disk_total_gb']} GB "
            f"({stats['disk_percent']}%) — {stats['disk_free_gb']} GB free"
        )

    # Bot connections
    from tunedrop.app.core.client import get_all_clients
    clients = get_all_clients()
    lines.append(f"\n🤖 <b>Bots:</b> {len(clients)} connected")

    # Event loop
    try:
        loop = __import__("asyncio").get_event_loop()
        lines.append(f"🔄 <b>Loop:</b> {type(loop).__name__}")
    except Exception:
        pass

    return "\n".join(lines)


async def build_storage_text() -> str:
    from tunedrop.app.utils.server_utils import get_storage_breakdown, get_system_stats

    breakdown = get_storage_breakdown()
    stats = get_system_stats()

    lines = ["<b>💾 Storage Manager</b>\n"]

    # Disk overview
    if "disk_used_gb" in stats:
        lines.append(
            f"💿 <b>Disk:</b> {stats['disk_used_gb']}/{stats['disk_total_gb']} GB "
            f"({stats['disk_percent']}% used)\n"
        )

    # Directory breakdown
    total_size = 0
    total_files = 0
    for name, info in breakdown.items():
        total_size += info["size"]
        total_files += info["files"]
        icon = {"temp": "📁", "songs": "🎵", "playlists": "📋", "zip": "📦", "logs": "📜", "data": "💾"}.get(name, "📄")
        lines.append(f"{icon} <b>{name}/</b>  {info['human']}  ({info['files']} files)")

    lines.append(f"\n<b>Total:</b> {total_size / (1024**2):.1f} MB in {total_files} files")
    lines.append("\n<i>Cleanup removes temp/playlist/zip files (safe during idle).</i>")

    return "\n".join(lines)


async def build_active_tasks_text() -> str:
    from tunedrop.app.services.progress import task_registry

    active = task_registry.active_count
    queued = task_registry.queued_count
    total_tasks = len(task_registry._tasks)

    lines = ["<b>📋 Active Tasks</b>\n"]
    lines.append(f"<b>Running:</b> {active}  |  <b>Queued:</b> {queued}  |  <b>Total tracked:</b> {total_tasks}")

    if not task_registry._tasks:
        lines.append("\n<i>No tasks running.</i>")
        return "\n".join(lines)

    # Show up to 10 active tasks
    shown = 0
    for task_id, task in list(task_registry._tasks.items()):
        if shown >= 10:
            remaining = total_tasks - shown
            lines.append(f"\n<i>... and {remaining} more</i>")
            break
        if task.worker and not task.worker.done():
            shown += 1
            source = task.request.source[:40] if hasattr(task.request, 'source') else "?"
            status = task.last_text[:50].replace("<", "&lt;").replace(">", "&gt;") if task.last_text else "starting..."
            lines.append(
                f"\n<b>#{shown}</b> <code>{task_id[:8]}</code> · user <code>{task.user_id}</code>\n"
                f"  📎 {source}\n"
                f"  📊 {status}"
            )

    if shown == 0:
        lines.append("\n<i>No actively running tasks (all queued or finishing).</i>")

    return "\n".join(lines)


def keyboard_to_dict(keyboard: InlineKeyboardMarkup) -> list[list[dict]]:
    """Convert an InlineKeyboardMarkup to a comparable dictionary structure."""
    result = []
    for row in keyboard.inline_keyboard:
        row_data = []
        for button in row:
            row_data.append({
                'text': button.text,
                'callback_data': getattr(button, 'callback_data', None),
            })
        result.append(row_data)
    return result
