from __future__ import annotations

import argparse
import shutil
import subprocess

from eks_harness.cli.ui import EXIT_ERROR, EXIT_USAGE, console, fail, ok, print_json, table
from eks_harness.paths import resolve_paths

PLAYWRIGHT_SPEC = "playwright@^1.55.0"


def cmd_workers(args: argparse.Namespace) -> int:
    from eks_harness.drivers import WorkerManager

    handles = WorkerManager(resolve_paths()).list()
    if args.json:
        print_json({"items": [h.as_dict() for h in handles]})
        return 0
    console.print(table(["kind", "lease", "pid", "url", "extensions"],
                        [(h.kind, h.key, h.pid, h.base_url, ", ".join(h.extra.get("extensions") or []))
                         for h in handles]))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    from eks_harness.drivers import WorkerManager

    manager = WorkerManager(resolve_paths())
    if args.all:
        stopped = manager.stop_all()
    elif args.kind and args.lease:
        stopped = int(manager.stop(args.kind, args.lease))
    else:
        fail("pass --all or <kind> <lease sid>", EXIT_USAGE)
    if args.json:
        print_json({"stopped": stopped})
    else:
        ok(f"stopped {stopped} worker(s)")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from eks_harness.drivers.profile import node_modules_dir

    if args.target != "web":
        fail("only the web driver has npm dependencies (playwright)", EXIT_USAGE)
    npm = shutil.which("npm")
    if not npm:
        fail("npm is not on PATH", EXIT_ERROR)
    target = node_modules_dir(resolve_paths())
    target.mkdir(parents=True, exist_ok=True)
    package = target / "package.json"
    if not package.exists():
        package.write_text('{"name": "eks-harness-drivers", "private": true}\n', encoding="utf-8")
    result = subprocess.run([npm, "install", "--no-audit", "--no-fund", "--prefix", str(target),
                             args.spec or PLAYWRIGHT_SPEC], capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"npm install failed:\n{(result.stderr or result.stdout)[-1500:]}", EXIT_ERROR)
    if args.json:
        print_json({"path": str(target), "spec": args.spec or PLAYWRIGHT_SPEC})
    else:
        ok(f"playwright installed for the web driver in {target}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("driver", help="driver workers: list, stop, install dependencies",
                                   description="Driver workers are Node processes, one per lease, that drive a "
                                               "browser profile or a React Native app for flows, scores and MCP.")
    sub = parser.add_subparsers(dest="driver_command", metavar="<subcommand>", required=True)
    p = sub.add_parser("workers", help="running driver workers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_workers)
    p = sub.add_parser("stop", help="stop driver workers")
    p.add_argument("kind", nargs="?", choices=["web", "mobile"])
    p.add_argument("lease", nargs="?", help="lease sid")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("install", help="install a driver's npm dependencies into the harness cache")
    p.add_argument("target", choices=["web"])
    p.add_argument("--spec", help=f"npm spec (default {PLAYWRIGHT_SPEC})")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_install)
