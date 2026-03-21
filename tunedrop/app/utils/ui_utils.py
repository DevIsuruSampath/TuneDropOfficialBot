from __future__ import annotations

from enum import Enum

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.utils.time_utils import format_bytes, format_duration_mmss, format_seconds


class DownloadPhase(Enum):
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    UPLOADING = "uploading"


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_progress_message(
    phase: DownloadPhase,
    percentage: float | None = None,
    details: str | None = None,
) -> str:
    if phase == DownloadPhase.SEARCHING:
        lines = ["<b>🔍 Searching...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.UPLOADING:
        return "<b>📤 Uploading...</b>"

    # DOWNLOADING and CONVERTING both show as "Processing"
    lines = ["<b>⚙️ Processing audio...</b>"]
    if percentage is not None:
        lines.append(f"<code>{percentage:.0f}%</code>")
    if details:
        lines.append(f"<i>{escape_html(details)}</i>")
    return "\n".join(lines)


def build_completion_message() -> str:
    return "<b>✅ Ready</b>"


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


def build_audio_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    bot_link = f"https://t.me/{bot_username}?start=share"
    share_url = f"https://t.me/share/url?url={bot_link}&text=Download%20songs%20instantly%20with%20TuneDrop%20%F0%9F%8E%A7"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎧 Try TuneDrop", url=bot_link),
            InlineKeyboardButton("📤 Share", url=share_url),
        ],
    ])


def build_playlist_completion(
    track_count: int,
    file_size: int,
    download_link: str,
    estimated_time: int,
    speed_kbps: float,
) -> str:
    return "\n".join([
        "<b>✅ Playlist ready</b>",
        "",
        f"<code>{track_count}</code> tracks · <code>{format_bytes(file_size)}</code>",
        f"<i>~{format_seconds(estimated_time)} at {speed_kbps:.0f} KB/s</i>",
        "",
        f"<code>{download_link}</code>",
    ])


def build_error_message(error: str) -> str:
    return "<b>❌ Failed. Try again.</b>"


def build_welcome_message() -> str:
    return "\n".join([
        "<b>TuneDrop</b>",
        "",
        "<i>Download any song in seconds</i>",
        "",
        "Send a <b>Spotify</b> or <b>YouTube</b> link",
        "or search with <code>/song</code> <i>name</i>",
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


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Cancel", callback_data="cancel")],
    ])


def build_retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Try Again", callback_data="retry")],
    ])
