from __future__ import annotations

import argparse
from pathlib import Path

from eks_harness.cli.ui import (
    EXIT_ERROR,
    EXIT_USAGE,
    fail,
    ok,
    print_json,
    warn,
    write_line,
)

DEFAULT_OUT = Path("docs") / "reference"
COOKBOOK = "cookbook.md"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "docs", help="generate the Markdown reference from the built-in registry",
        description="Write the generated reference (ops, catalogs, checks, presets, resources, plan IR and CLI) as "
                    "Markdown, or check that a directory holds the current version of it. Only built-in "
                    "contributions are documented; plugins are not loaded.")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="directory of the reference files (default: docs/reference)")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 and list the files that differ from the generated ones")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=cmd_docs)


def cmd_docs(args: argparse.Namespace) -> int:
    from eks_harness.docs import check_reference, write_reference
    from eks_harness.docs.quotes import stale_quotes, sync_file

    out = Path(args.out).expanduser()
    if out.exists() and not out.is_dir():
        fail(f"--out {out} is not a directory", EXIT_USAGE)
    cookbook = out.parent / COOKBOOK
    root = out.parent.parent
    if args.check:
        differing = check_reference(out)
        if cookbook.is_file():
            differing += [f"{COOKBOOK}: {path}" for path in stale_quotes(cookbook.read_text(encoding="utf-8"), root)]
        if args.json:
            print_json({"out": str(out), "ok": not differing, "differing": differing})
        elif differing:
            warn(f"{len(differing)} reference file(s) in {out} differ from the generated reference; "
                 f"run eks-harness docs --out {out}")
            for name in differing:
                write_line(name)
        else:
            ok(f"{out} is up to date")
        return EXIT_ERROR if differing else 0
    written = write_reference(out)
    if cookbook.is_file() and sync_file(cookbook, root):
        written.append(cookbook)
    if args.json:
        print_json({"out": str(out), "written": [str(path) for path in written]})
    else:
        for path in written:
            write_line(str(path))
    return 0


__all__ = ["cmd_docs", "register"]
