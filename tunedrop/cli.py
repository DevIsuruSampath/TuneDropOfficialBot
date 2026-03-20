from __future__ import annotations

import argparse

from tunedrop.runner import run_all, run_with_signal_handling


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m tunedrop",
        description="Run the TuneDrop Telegram music downloader project.",
    )


def main() -> int:
    build_parser().parse_args()
    run_with_signal_handling(run_all())
    return 0
