from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tunedrop",
        description="Run the TuneDrop Telegram bot and web server.",
    )
    parser.add_argument(
        "--mode",
        choices=("bot", "web", "all"),
        default="all",
        help="Runtime mode to start. Defaults to both bot and web server.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    from tunedrop.app.runtime import run

    run(args.mode)
    return 0
