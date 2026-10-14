from __future__ import annotations

import argparse
from dataclasses import dataclass

from eks_harness.cli.ui import confirm_or_exit, info, print_json, table, write_line
from eks_harness.drivers.procs import (
    ProcRegistry,
    kill_group,
    process_start,
    reap,
    scan_orphans,
)
from eks_harness.paths import resolve_paths


@dataclass(frozen=True, slots=True)
class Found:
    pid: int
    kind: str
    source: str
    command: str


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "reap", help="stop harness processes whose owner is gone",
        description="Stop driver workers and dev servers the harness started whose owner process is gone. With "
                    "--all-installs it also finds orphans of other eks-harness installs (uvx, uv tool, build "
                    "environments, in-repo checkouts): driver worker servers and .harness dev servers re-parented to "
                    "init. Lists them first and stops them after confirmation.")
    parser.add_argument("--all-installs", action="store_true",
                        help="also scan every process re-parented to init for harness workers and dev servers")
    parser.add_argument("--include-dev", action="store_true",
                        help="with --all-installs, also list next dev, expo, metro and vite servers re-parented to "
                             "init even without a .harness path in their command line")
    parser.add_argument("--dry-run", action="store_true", help="only list what would be stopped")
    parser.add_argument("--yes", action="store_true", help="stop without asking")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=cmd_reap)


def candidates(all_installs: bool, include_dev: bool) -> tuple[list[Found], ProcRegistry]:
    registry = ProcRegistry.for_paths(resolve_paths())
    found: list[Found] = []
    for record in registry.records():
        if record.running() and not record.owner.alive():
            found.append(Found(record.pid, record.kind, "registry", " ".join(record.argv)))
    if all_installs:
        known = {item.pid for item in found}
        for orphan in scan_orphans(include_dev=include_dev):
            if orphan.pid not in known:
                found.append(Found(orphan.pid, orphan.kind, "scan", orphan.command))
    return found, registry


def cmd_reap(args: argparse.Namespace) -> int:
    found, registry = candidates(args.all_installs, args.include_dev)
    listing = [{"pid": item.pid, "kind": item.kind, "source": item.source, "command": item.command} for item in found]
    if not found:
        reap(registry)
        if args.json:
            print_json({"orphans": [], "stopped": []})
        else:
            info("no orphaned harness processes")
        return 0
    if not args.json:
        write_line(f"{len(found)} orphaned harness process{'es' if len(found) != 1 else ''}:")
        from eks_harness.cli.ops_cmds import plain_rows, wide_console

        wide_console().print(table(["pid", "kind", "source", "command"],
                                   plain_rows((item.pid, item.kind, item.source, item.command[:160])
                                              for item in found)))
    if args.dry_run:
        if args.json:
            print_json({"orphans": listing, "stopped": []})
        return 0
    confirm_or_exit(f"Stop these {len(found)} processes", args.yes)
    stopped: list[int] = []
    for item in found:
        started = process_start(item.pid)
        if started is not None and kill_group(item.pid):
            stopped.append(item.pid)
    reap(registry)
    if args.json:
        print_json({"orphans": listing, "stopped": stopped})
    else:
        info(f"stopped {len(stopped)} of {len(found)}")
    return 0


__all__ = ["Found", "candidates", "cmd_reap", "register"]
