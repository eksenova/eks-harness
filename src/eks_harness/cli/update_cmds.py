from __future__ import annotations

import argparse
import subprocess
import sys
import time

from eks_harness import selfupdate, service
from eks_harness.cli.ui import (
    EXIT_ERROR,
    EXIT_OK,
    console,
    fail,
    info,
    kv_panel,
    ok,
    print_json,
    warn,
)
from eks_harness.config import load as load_config
from eks_harness.daemon import server
from eks_harness.paths import resolve_paths

DEFAULT_EXTRAS = ["video", "html"]


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "unknown"


def _show(status: selfupdate.Status) -> None:
    current = status.installed
    rows = [("Installed", f"{current.version} {_short(current.commit)}{' (modified)' if current.dirty else ''}"),
            ("Source", f"{current.source}{': ' + current.location if current.location else ''}"),
            ("Extras", ", ".join(current.extras) or "none"),
            ("Follows", f"{status.repository} {status.ref}"),
            ("Latest", _short(status.latest)),
            ("Status", ("update available: " if status.available else "") + status.reason)]
    console.print(kv_panel("eks-harness update", rows))
    for change in status.changes[:20]:
        console.print(f"  [dim]{change['sha'][:8]}[/dim] {change['title']}")
    if len(status.changes) > 20:
        console.print(f"  [dim]... and {len(status.changes) - 20} more[/dim]")


def _restart_services(paths, restart: bool, as_json: bool) -> dict:
    done: dict = {}
    if not restart:
        return done
    running = server.running_daemon(paths)
    if running:
        command = [sys.executable, "-m", "eks_harness.cli", "daemon", "restart"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        done["daemon"] = "restarted" if result.returncode == 0 else f"restart failed: {result.stderr.strip()[-300:]}"
    if service.installed(paths, service.NODE):
        done["node"] = "restarted" if service.restart(paths, service.NODE) else "restart failed"
    if not as_json:
        for name, state in done.items():
            (ok if state == "restarted" else warn)(f"{name}: {state}")
    return done


def cmd_update(args: argparse.Namespace) -> int:
    paths = resolve_paths().ensure()
    config = load_config(paths)
    repository = args.repository or config["update.repository"]
    ref = args.ref or config["update.ref"]
    try:
        status = selfupdate.check(repository, ref)
    except selfupdate.UpdateError as problem:
        fail(str(problem), EXIT_ERROR)
    if args.check:
        if args.json:
            print_json(status.as_dict())
        else:
            _show(status)
        return EXIT_OK
    if not args.json:
        _show(status)
    if not status.available and not args.force:
        if args.json:
            print_json({**status.as_dict(), "updated": False})
        elif status.installed.source in ("editable", "directory"):
            info("nothing installed; pass --force to replace this install with the GitHub build of "
                 f"{_short(status.latest)}")
        else:
            ok("eks-harness is up to date")
        return EXIT_OK
    if selfupdate.receipt_path() is None:
        fail("this eks-harness was not installed with uv tool install, so it cannot update itself; "
             f"install it with: uv tool install --link-mode copy \"{selfupdate.requirement(repository, status.latest, DEFAULT_EXTRAS)}\"",
             EXIT_ERROR)
    extras = args.extras.split(",") if args.extras is not None else (status.installed.extras or DEFAULT_EXTRAS)
    extras = [e.strip() for e in extras if e.strip()]
    started = time.time()
    if not args.json:
        info(f"installing {_short(status.latest)} from {repository} (builds the web UI; takes a minute or two)")
    try:
        with selfupdate.update_lock(paths):
            selfupdate.install(repository, status.latest, extras,
                               output=(lambda line: console.print(f"[dim]{line}[/dim]")) if args.verbose else None)
    except selfupdate.UpdateError as problem:
        selfupdate.write_state(paths, failed={"commit": status.latest, "error": str(problem), "at": time.time()})
        fail(str(problem), EXIT_ERROR)
    selfupdate.write_state(paths, failed=None, lastUpdate={"from": status.installed.commit, "to": status.latest,
                                                          "at": time.time(), "by": "cli"})
    if not args.json:
        ok(f"installed {_short(status.latest)} in {time.time() - started:.0f}s")
    restarted = _restart_services(paths, not args.no_restart, args.json)
    if args.json:
        print_json({**status.as_dict(), "updated": True, "restarted": restarted})
    return EXIT_OK


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "update", help="update eks-harness from its GitHub source and restart the daemon",
        description="Installs the latest commit of update.ref from update.repository with uv tool install (the "
                    "installed extras are kept) and restarts the daemon gracefully and the node agent. The daemon "
                    "also updates itself when update.auto is on and nothing is running.")
    parser.add_argument("--check", action="store_true", help="only show whether an update is available")
    parser.add_argument("--ref", help="branch, tag or commit to install (default: update.ref)")
    parser.add_argument("--repository", help="git repository URL (default: update.repository)")
    parser.add_argument("--extras", help="comma separated extras to install (default: the installed ones)")
    parser.add_argument("--force", action="store_true",
                        help="install even when up to date or when this install comes from a local checkout")
    parser.add_argument("--no-restart", action="store_true", help="do not restart the daemon or the node agent")
    parser.add_argument("--verbose", action="store_true", help="show the uv and build output")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=cmd_update)
