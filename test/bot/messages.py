WELCOME_TEXT = """
Welcome to the Music Downloader Bot.

Send any of these:
- Spotify track URL
- Spotify playlist URL
- YouTube URL
- YouTube Music URL
- /song <song name>

Commands:
/help - show usage
/myfiles - show your recent playlist files
/cancel - cancel the current task
""".strip()


HELP_TEXT = """
Usage:

1. Send a supported music URL directly.
2. Use /song <name> to search by song title.
3. Use /myfiles to get links to your recent playlist ZIP files.
4. Use /cancel to stop the current job.

Playlist flow:
- The bot downloads all tracks
- Creates a ZIP archive
- Uploads it to the private storage channel
- Returns file size, estimated download time, and a web link
""".strip()
