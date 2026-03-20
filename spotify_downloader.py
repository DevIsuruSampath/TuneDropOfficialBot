#!/usr/bin/env python3
import shutil
import subprocess
import sys
from pathlib import Path

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
OUTPUT_TEMPLATE = str(DOWNLOAD_DIR / "{artist} - {title}.{output-ext}")


def check_binary(name: str) -> bool:
    return shutil.which(name) is not None


def ensure_requirements() -> None:
    missing = []
    if not check_binary("spotdl"):
        missing.append("spotdl")
    if not check_binary("ffmpeg"):
        missing.append("ffmpeg")

    if missing:
        print("Missing required tools:", ", ".join(missing))
        print("Install them first, then run this script again.")
        print()
        print("Examples:")
        print("  pip install spotdl")
        print("  sudo apt install ffmpeg")
        sys.exit(1)


def download_spotify(url: str) -> int:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        "spotdl",
        "download",
        url,
        "--format",
        "mp3",
        "--output",
        OUTPUT_TEMPLATE,
    ]

    print("Running:", " ".join(cmd))
    print(f"Saving files to: {DOWNLOAD_DIR}")
    print()

    result = subprocess.run(cmd)
    return result.returncode


def main() -> None:
    ensure_requirements()

    print("Spotify -> MP3 Downloader")
    print("Uses spotdl + ffmpeg")
    print()

    url = input("Enter Spotify track/album/playlist URL: ").strip()
    if not url:
        print("No URL provided.")
        sys.exit(1)

    code = download_spotify(url)
    if code == 0:
        print()
        print("Download completed successfully.")
    else:
        print()
        print(f"Download failed with exit code {code}.")
        sys.exit(code)


if __name__ == "__main__":
    main()
