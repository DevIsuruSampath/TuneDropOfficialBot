from __future__ import annotations

from enum import StrEnum

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.utils.time_utils import format_bytes, format_duration_mmss, format_seconds


class DownloadPhase(StrEnum):
    QUEUED = "queued"
    SEARCHING = "searching"
    CHECKING_CACHE = "checking_cache"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    PACKAGING = "packaging"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_progress_message(
    phase: DownloadPhase,
    percentage: float | None = None,
    details: str | None = None,
    eta: float | None = None,
    speed_kbps: float | None = None,
) -> str:
    if phase == DownloadPhase.QUEUED:
        lines = ["<b>⏳ Queued</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.SEARCHING:
        lines = ["<b>🔍 Searching...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.CHECKING_CACHE:
        lines = ["<b>🧠 Checking cache...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.DOWNLOADING:
        lines = ["<b>⬇️ Downloading...</b>"]
        if percentage is not None:
            pct_str = f"{percentage:.0f}%"
            parts = [pct_str]
            if speed_kbps is not None and speed_kbps > 0:
                parts.append(f"{speed_kbps:.0f} KB/s")
            if eta is not None and eta > 0:
                parts.append(f"{format_seconds(int(eta))} left")
            lines.append(f"<code>{'  ·  '.join(parts)}</code>")
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.CONVERTING:
        lines = ["<b>🔄 Converting audio...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.PACKAGING:
        lines = ["<b>📦 Creating ZIP archive...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.UPLOADING:
        return "<b>📤 Uploading...</b>"

    if phase == DownloadPhase.COMPLETED:
        return "<b>✅ Ready</b>"

    if phase == DownloadPhase.FAILED:
        return "<b>❌ Failed</b>"

    if phase == DownloadPhase.CANCELLED:
        return "<b>🚫 Cancelled</b>"

    return f"<b>{escape_html(phase.value)}</b>"


def build_audio_caption(
    title: str,
    artist: str,
    duration: int,
    quality: str = "320kbps",
) -> str:
    return (
        f"🎵 <b>{escape_html(title)}</b>\n"
        f"👤 {escape_html(artist)}\n\n"
        f"⏱ {format_duration_mmss(duration)}   🎧 {quality}"
    )


def build_audio_keyboard(bot_username: str, download_url: str | None = None) -> InlineKeyboardMarkup:
    bot_link = f"https://t.me/{bot_username}?start=share"
    share_url = f"https://t.me/share/url?url={bot_link}&text=Download%20songs%20instantly%20with%20TuneDrop%20%F0%9F%8E%A7"
    buttons = []
    if download_url:
        buttons.append([InlineKeyboardButton("⬇️ Download", url=download_url)])
    buttons.append([
        InlineKeyboardButton("🎧 Try TuneDrop", url=bot_link),
        InlineKeyboardButton("📤 Share", url=share_url),
    ])
    return InlineKeyboardMarkup(buttons)


def build_playlist_status(
    phase: DownloadPhase,
    done: int,
    total: int,
    cached: int = 0,
    downloading: int = 0,
    failed: int = 0,
) -> str:
    """Build playlist progress message.

    ⏳ Processing playlist

    Stage: Checking cache
    Progress: 32/64
    Cached: 20
    """
    _phase_label = {
        DownloadPhase.SEARCHING: "Looking up",
        DownloadPhase.CHECKING_CACHE: "Checking cache",
        DownloadPhase.DOWNLOADING: "Downloading",
        DownloadPhase.CONVERTING: "Converting audio",
        DownloadPhase.PACKAGING: "Creating ZIP",
        DownloadPhase.UPLOADING: "Uploading",
    }
    stage = _phase_label.get(phase, phase.value.capitalize())

    # Clamp to prevent 65/64 overflow
    done = min(done, total) if total > 0 else done

    lines = ["<b>⏳ Processing playlist</b>", ""]
    lines.append(f"📦 Stage: <b>{stage}</b>")
    if total > 0:
        lines.append(f"📊 Progress: <b>{done}/{total}</b>")
    if cached > 0:
        lines.append(f"⚡ Cached: {cached}")
    if downloading > 0:
        lines.append(f"⬇️ Downloaded: {downloading}")
    if failed > 0:
        lines.append(f"❌ Failed: {failed}")
    return "\n".join(lines)


def build_playlist_completion(
    track_count: int,
    file_size: int,
    download_link: str,
    *,
    cached_count: int = 0,
    downloaded_count: int = 0,
    failed_count: int = 0,
) -> str:
    """Build playlist completion message.

    ✅ Playlist Ready

    🎶 Tracks: 64
    💾 Size: 361.93 MB
    ⚡ Cached: 64

    🔗 Download: https://tdrp.cc/generate/xxxx
    """
    lines = [
        "<b>✅ Playlist Ready</b>",
        "",
        f"🎶 Tracks: <b>{track_count}</b>",
        f"💾 Size: <b>{format_bytes(file_size)}</b>",
    ]
    if cached_count > 0:
        lines.append(f"⚡ Cached: {cached_count}")
    lines.append(f"❌ Failed: {failed_count}")
    lines.append("")
    lines.append(f"🔗 <a href=\"{download_link}\">Download</a>")
    return "\n".join(lines)


def build_error_message(error: str) -> str:
    return f"<b>❌ Failed. Try again.</b>\n\n<i>{escape_html(error)}</i>"


def build_large_file_message(
    title: str,
    artist: str,
    duration: int,
    file_size: int,
    download_link: str,
    estimated_time: int,
    speed_kbps: float,
) -> str:
    return "\n".join([
        f"🎵 <b>{escape_html(title)}</b>",
        f"👤 {escape_html(artist)}",
        "",
        f"<code>{format_bytes(file_size)}</code> · ⏱ {format_duration_mmss(duration)}",
        f"<i>~{format_seconds(estimated_time)} at {speed_kbps:.0f} KB/s</i>",
        "",
        f"<code>{download_link}</code>",
    ])


def build_welcome_message() -> str:
    return "\n".join([
        "<b>🎵 Welcome to TuneDrop!</b>",
        "",
        "Download any song or playlist instantly.",
        "",
        "• Send a <b>Spotify</b> or <b>YouTube</b> link",
        "• Use <code>/song</code> <i>name</i> to search",
        "• Supports tracks, playlists & YouTube Music",
        "• <b>320kbps</b> MP3 with album art",
        "",
        "<code>/help</code> · <code>/myfiles</code> · <code>/cancel</code>",
    ])


def build_help_message() -> str:
    return "\n".join([
        "<b>How to use TuneDrop</b>",
        "",
        "<b>Download a song</b>",
        "Send a Spotify / YouTube URL",
        "or use <code>/song</code> <i>name</i>",
        "",
        "<b>Download a playlist</b>",
        "Send a playlist URL",
        "all tracks packed into a ZIP",
        "",
        "<code>/myfiles</code> recent downloads",
        "<code>/cancel</code> stop current task",
    ])


def build_welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 Search", callback_data="show_search"),
            InlineKeyboardButton("❓ Help", callback_data="show_help"),
        ],
    ])


def build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_start")],
    ])


def build_retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Try Again", callback_data="retry")],
    ])


def build_force_sub_message(channel_link: str) -> tuple[str, InlineKeyboardMarkup]:
    """Return (text, markup) for force-subscription prompt."""
    text = (
        "<b>🔒 Subscription Required</b>\n\n"
        "Join our channel to use TuneDrop.\n"
        "After joining, send your request again."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=channel_link)],
    ])
    return text, markup
