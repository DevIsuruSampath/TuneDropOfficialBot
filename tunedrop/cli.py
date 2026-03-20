from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tunedrop",
        description="Run the TuneDrop Telegram bot or web runtime.",
    )
    parser.add_argument(
        "--mode",
        choices=("bot", "web", "all"),
        default="bot",
        help="Runtime mode to start. Defaults to the Telegram bot only.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    from app.runtime import run

    run(args.mode)
    return 0
