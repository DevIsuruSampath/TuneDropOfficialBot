from __future__ import annotations

from enum import Enum

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tunedrop.app.utils.time_utils import format_bytes, format_duration_mmss, format_seconds


class DownloadPhase(Enum):
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    UPLOADING = "uploading"


_PHASE_CONFIG = {
    DownloadPhase.SEARCHING: {"emoji": "🎯", "label": "Searching"},
    DownloadPhase.DOWNLOADING: {"emoji": "📥", "label": "Downloading"},
    DownloadPhase.CONVERTING: {"emoji": "🎛️", "label": "Converting"},
    DownloadPhase.UPLOADING: {"emoji": "📤", "label": "Uploading"},
}


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_progress_message(
    phase: DownloadPhase,
    percentage: float | None = None,
    details: str | None = None,
) -> str:
    config = _PHASE_CONFIG[phase]
    lines = [f"<b>{config['emoji']} {config['label']}</b>"]

    if percentage is not None:
        lines.append(f"<code>{percentage:.0f}%</code>")
    if details:
        lines.append(f"<i>{escape_html(details)}</i>")

    if phase == DownloadPhase.SEARCHING:
        lines.append("")
        lines.append("<i>Finding your track across multiple sources...</i>")
    elif phase == DownloadPhase.CONVERTING:
        lines.append("")
        lines.append("<i>Converting to MP3 (320kbps)...</i>")

    return "\n".join(lines)


def build_completion_card(
    title: str,
    artist: str,
    duration: int,
    file_size: int,
    quality: str = "320kbps",
) -> str:
    return "\n".join([
        "<b>✅ Download Complete</b>",
        "",
        f"<b>🎵 {escape_html(title)}</b>",
        f"<i>{escape_html(artist)}</i>",
        "",
        f"⏱ <b>Duration:</b> <code>{format_duration_mmss(duration)}</code>",
        f"📦 <b>Size:</b> <code>{format_bytes(file_size)}</code>",
        f"🎧 <b>Quality:</b> <code>MP3 {quality}</code>",
        "",
        "<i>Delivered by TuneDrop 🎶</i>",
    ])


def build_audio_caption(
    title: str,
    artist: str,
    duration: int,
    quality: str = "320kbps",
) -> str:
    return (
        f"<b>🎵 {escape_html(title)}</b> — <i>{escape_html(artist)}</i>\n"
        f"⏱ <code>{format_duration_mmss(duration)}</code> | 🎧 <code>{quality}</code>"
    )


def build_playlist_completion(
    track_count: int,
    file_size: int,
    download_link: str,
    estimated_time: int,
    speed_kbps: float,
) -> str:
    return "\n".join([
        "<b>✅ Playlist Ready</b>",
        "",
        f"🎵 <b>Tracks:</b> <code>{track_count}</code>",
        f"📦 <b>ZIP Size:</b> <code>{format_bytes(file_size)}</code>",
        f"⏱ <b>Est. Time:</b> <code>{format_seconds(estimated_time)}</code> at {speed_kbps:.0f} KB/s",
        "",
        f"<b>📥 Download:</b>",
        f"<code>{download_link}</code>",
    ])


def build_error_message(error: str) -> str:
    friendly = _translate_error(error)
    suggestions = _get_suggestions(error)
    lines = [
        "<b>⚠️ Download Failed</b>",
        "",
        "<i>Sorry, something went wrong.</i>",
        "",
        "<b>What happened:</b>",
        f"<code>{escape_html(friendly)}</code>",
    ]
    if suggestions:
        lines.append("")
        lines.append("<b>Try this:</b>")
        for s in suggestions:
            lines.append(f"• {s}")
    lines.extend(["", "<i>Need help? Use /help</i>"])
    return "\n".join(lines)


def _translate_error(error: str) -> str:
    lowered = error.lower()
    if "timeout" in lowered or "stalled" in lowered:
        return "Download took too long. The server might be busy."
    if "not found" in lowered or "unavailable" in lowered:
        return "This track isn't available on our sources."
    if "ffmpeg" in lowered or "conversion" in lowered:
        return "Couldn't convert the audio file."
    if "rate limit" in lowered:
        return "Too many requests. Please wait a moment."
    return error[:200]


def _get_suggestions(error: str) -> list[str]:
    lowered = error.lower()
    if "not found" in lowered or "unavailable" in lowered:
        return [
            "Check the URL is correct",
            "Try /song &lt;name&gt; to search instead",
            "The track might be region-locked",
        ]
    if "timeout" in lowered or "stalled" in lowered:
        return [
            "Try again in a few moments",
            "Use /cancel and retry",
        ]
    return [
        'Tap "Try Again" below',
        "Try /song &lt;name&gt; to search",
        "Use /help for more info",
    ]


def build_welcome_message() -> str:
    return "\n".join([
        "<b>🎵 Welcome to TuneDrop</b>",
        "",
        "<i>Your premium music downloader</i>",
        "",
        "<b>🎯 Quick Start</b>",
        "",
        "• Send Spotify / YouTube URL",
        "• Use <code>/song</code> <i>name</i> to search",
        "• Playlists → ZIP archive",
        "",
        "<b>⚙️ Commands</b>",
        "<code>/help</code> — Usage guide",
        "<code>/myfiles</code> — Recent downloads",
        "<code>/cancel</code> — Stop current task",
        "",
        "<i>Fast • Free • No ads</i>",
    ])


def build_help_message() -> str:
    return "\n".join([
        "<b>📖 TuneDrop — Help</b>",
        "",
        "<b>🎵 Download a Song</b>",
        "• Send a Spotify / YouTube / YouTube Music URL",
        "• Or use <code>/song</code> <i>name</i>",
        "",
        "<b>📦 Playlists</b>",
        "• Send a playlist URL — all tracks packed into ZIP",
        "• <code>/myfiles</code> to get your download links",
        "",
        "<b>✅ Controls</b>",
        "• <code>/cancel</code> or tap the Cancel button",
        "• Tap Try Again on a failed download",
    ])


def build_welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 Search Song", callback_data="show_search"),
            InlineKeyboardButton("❓ Help", callback_data="show_help"),
        ],
    ])


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def build_retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Try Again", callback_data="retry")],
    ])
