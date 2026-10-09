from __future__ import annotations

import argparse


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("video", help="render, validate, scaffold and review videos",
                                   description="Video engine commands: render, validate, schema, sync, doctor, init, and review: sheet, check.")
    from eks_harness.video.cli.parser import configure

    configure(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from eks_harness.video.cli.__main__ import run as run_video

    return run_video(args)
