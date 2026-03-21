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
        lines = ["<b>🔍 Searching</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.UPLOADING:
        return "<b>📤 Uploading</b>"

    # DOWNLOADING and CONVERTING both show as "Processing"
    lines = ["<b>⚙️ Processing audio</b>"]
    if percentage is not None:
        lines.append(f"<code>{percentage:.0f}%</code>")
    if details:
        lines.append(f"<i>{escape_html(details)}</i>")
    return "\n".join(lines)


def build_completion_message() -> str:
    return "<b>✅ Sent</b>"


def build_audio_caption(
    title: str,
    artist: str,
    duration: int,
    quality: str = "320kbps",
) -> str:
    return (
        f"<b>{escape_html(title)}</b>\n"
        f"<i>{escape_html(artist)}</i>\n"
        f"<code>{format_duration_mmss(duration)}</code> · <code>{quality}</code>"
    )


def build_audio_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_username}&text=TuneDrop%20%E2%80%93%20Download%20any%20song%20%E2%9C%94"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Share", url=share_url),
            InlineKeyboardButton("🤖 Open Bot", url=f"https://t.me/{bot_username}"),
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
    friendly = _translate_error(error)
    return "\n".join([
        "<b>Something went wrong</b>",
        "",
        f"<i>{escape_html(friendly)}</i>",
        "",
        'Tap "Try Again" below',
    ])


def _translate_error(error: str) -> str:
    lowered = error.lower()
    if "timeout" in lowered or "stalled" in lowered:
        return "Download timed out. Try again."
    if "not found" in lowered or "unavailable" in lowered:
        return "Track not available."
    if "ffmpeg" in lowered or "conversion" in lowered:
        return "Couldn't convert the audio."
    if "rate limit" in lowered:
        return "Too many requests. Wait a moment."
    return error[:200]


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
