from __future__ import annotations

from datetime import UTC, datetime

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
        lines = ["<b>⏳ In queue</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.SEARCHING:
        lines = ["<b>🔍 Searching...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.CHECKING_CACHE:
        lines = ["<b>⚡ Checking cache...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.DOWNLOADING:
        lines = ["<b>⬇️ Downloading</b>"]
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
        lines = ["<b>🔄 Converting to MP3...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.PACKAGING:
        lines = ["<b>📦 Packing ZIP...</b>"]
        if details:
            lines.append(f"<i>{escape_html(details)}</i>")
        return "\n".join(lines)

    if phase == DownloadPhase.UPLOADING:
        return "<b>📤 Uploading...</b>"

    if phase == DownloadPhase.COMPLETED:
        return None

    if phase == DownloadPhase.FAILED:
        return "<b>❌ Something went wrong</b>"

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
        f"👤 {escape_html(artist)}\n"
        f"⏱ {format_duration_mmss(duration)}  ·  🎧 {quality}"
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
    lines.append(f"📦 <b>{stage}</b>")
    if total > 0:
        lines.append(f"📊 <b>{done}/{total}</b> tracks")
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
    download_link: str | None,
    *,
    cached_count: int = 0,
    downloaded_count: int = 0,
    failed_count: int = 0,
    track_links: list[tuple[str, str]] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build playlist completion message with download buttons.

    track_links: optional list of (title, download_url) for individual tracks
    in small playlists where no ZIP is created.
    """
    lines = [
        "<b>✅ Playlist ready!</b>",
        "",
    ]
    # Show actual files in ZIP as the main stat
    actual_files = cached_count + downloaded_count
    if failed_count > 0:
        lines.append(f"🎶 <b>{actual_files}/{track_count}</b> tracks  ·  💾 <b>{format_bytes(file_size)}</b>")
    else:
        lines.append(f"🎶 <b>{track_count}</b> tracks  ·  💾 <b>{format_bytes(file_size)}</b>")
    # Show breakdown only when it adds info beyond the total
    if cached_count > 0:
        lines.append(f"⚡ Cached: {cached_count}")
    if downloaded_count > 0:
        lines.append(f"⬇️ Downloaded: {downloaded_count}")
    if failed_count > 0:
        lines.append(f"❌ Failed: {failed_count}")

    buttons = []
    if download_link:
        buttons.append([InlineKeyboardButton("⬇️ Download ZIP", url=download_link)])
    elif track_links:
        # Small playlist — individual track download buttons (max 5)
        for title, url in track_links[:5]:
            # Truncate long titles for button label
            label = title[:30] + "…" if len(title) > 30 else title
            buttons.append([InlineKeyboardButton(f"⬇️ {label}", url=url)])

    if buttons:
        markup = InlineKeyboardMarkup(buttons)
    else:
        markup = InlineKeyboardMarkup([])

    return "\n".join(lines), markup


def build_error_message(error: str) -> str:
    return f"<b>❌ Something went wrong</b>\n<i>{escape_html(error)}</i>"


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
        f"💾 {format_bytes(file_size)}  ·  ⏱ {format_duration_mmss(duration)}",
        f"<i>~{format_seconds(estimated_time)} at {speed_kbps:.0f} KB/s</i>",
        "",
        f"<code>{download_link}</code>",
    ])


def build_welcome_message() -> str:
    return "\n".join([
        "<b>🎧 TuneDrop</b>",
        "",
        "Your music, delivered in seconds.",
        "Send a <b>Spotify</b> or <b>YouTube Music</b> link — get high-quality audio instantly.",
        "",
        "🎵 <b>Songs</b> — link or <code>/song</code> + name",
        "📀 <b>Playlists</b> — playlist link → ZIP archive",
        "",
        "👇 <i>Tap a button to start!</i>",
    ])


def build_help_message() -> str:
    return "\n".join([
        "<b>📥 How to use</b>",
        "",
        "🎵 <b>Songs</b>",
        "• Send a Spotify or YouTube Music link",
        "• Or type <code>/song</code> + song name",
        "• High-quality audio in seconds!",
        "",
        "📀 <b>Playlists</b>",
        "• Send a Spotify or YouTube Music playlist URL",
        "• All tracks packed into a ZIP",
        "",
        "<b>Commands</b>",
        "<code>/song</code> — search &amp; download",
        "<code>/myfiles</code> — your recent downloads",
        "<code>/account</code> — account &amp; Pro status",
        "<code>/cancel</code> — stop current task",
        "<code>/donation</code> — support TuneDrop ⭐",
    ])


def build_welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Search", callback_data="show_search"),
            InlineKeyboardButton("❓ Help", callback_data="show_help"),
        ],
        [
            InlineKeyboardButton("❤️ Support", callback_data="show_donation"),
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


def format_expiry(expires_at: datetime) -> str:
    """Format a Pro expiry datetime into a human-readable string."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    remaining = expires_at - now
    if remaining.total_seconds() <= 0:
        return "Expired"
    days = remaining.days
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''} left"
    hours = int(remaining.total_seconds() // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''} left"


def build_free_delivery_message(
    title: str,
    artist: str,
    duration: int,
    download_url: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a download-link-only message for Free users."""
    text = (
        f"✅ <b>Ready!</b>\n\n"
        f"🎵 <b>{escape_html(title)}</b>\n"
        f"👤 {escape_html(artist)}\n"
        f"⏱ {format_duration_mmss(duration)}  ·  🎧 320kbps\n\n"
        f"<i>⚡ Go Pro for instant delivery in Telegram + no ads</i>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download", url=download_url)],
        [InlineKeyboardButton("⭐ Get Pro", callback_data="show_donation")],
    ])
    return text, markup


def build_force_sub_message(channel_link: str) -> tuple[str, InlineKeyboardMarkup]:
    """Return (text, markup) for force-subscription prompt."""
    text = (
        "<b>🔒 Join to continue</b>\n\n"
        "Join our channel to use TuneDrop, then tap <b>Try Again</b>."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=channel_link)],
        [InlineKeyboardButton("✅ Try Again", callback_data="check_sub")],
    ])
    return text, markup
