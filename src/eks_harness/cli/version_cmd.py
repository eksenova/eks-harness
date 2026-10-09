from __future__ import annotations

import argparse
import platform
import sys

from eks_harness._version import build_info
from eks_harness.cli.ui import console, kv_panel, print_json


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("version", help="show the installed version and source hash")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    info = build_info()
    info["python"] = sys.version.split()[0]
    info["platform"] = platform.platform()
    if args.json:
        print_json(info)
        return 0
    rows = [("Version", info.get("version")), ("Source hash", info.get("sourceHash") or "unknown"),
            ("Built", info.get("builtAt") or "not built (source tree)"), ("Editable", info.get("editable")),
            ("Python", info["python"]), ("Platform", info["platform"])]
    if info.get("sourceRoot"):
        rows.append(("Source", info["sourceRoot"]))
    console.print(kv_panel("eks-harness", rows))
    return 0
