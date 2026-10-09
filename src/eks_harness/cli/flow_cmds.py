from __future__ import annotations

import argparse
from pathlib import Path

from eks_harness.cli.client import client_from_args
from eks_harness.cli.ui import EXIT_ERROR, EXIT_USAGE, console, fail, print_json, table
from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths

FLOW_FAILED = 1


def parse_sets(values: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            fail(f"--set takes KEY=VALUE (got {item!r})", EXIT_USAGE)
        key, _, value = item.partition("=")
        params[key.strip()] = value
    return params


def cmd_run(args: argparse.Namespace) -> int:
    from eks_harness.drivers.client import WorkerError
    from eks_harness.drivers.profile import ProfileError
    from eks_harness.flows import FlowError, FlowRequest, format_report, run_flow

    if not args.flow and not args.code:
        fail("pass a flow file or -e '<python>'", EXIT_USAGE)
    flow = Path(args.flow).expanduser() if args.flow else None
    if flow and not flow.is_file():
        fail(f"no such flow: {flow}", EXIT_USAGE)
    paths = resolve_paths()
    config = load_config(paths)
    request = FlowRequest(flow=flow, code=args.code, platform=args.platform, params=parse_sets(args.set),
                          fetch=not args.no_fetch, out=Path(args.out).resolve() if args.out else None,
                          verbose=args.verbose and not args.json, wait=args.wait, release=args.release,
                          instance=args.instance, session=args.session)
    with client_from_args(args) as client:
        try:
            report = run_flow(request, client=client, paths=paths, config=config)
        except (FlowError, ProfileError, WorkerError) as error:
            fail(str(error), EXIT_ERROR)
    if args.json:
        print_json(report)
    else:
        console.print(format_report(report, flow.stem if flow else "inline"), markup=False, highlight=False)
    return 0 if report["ok"] else FLOW_FAILED


def cmd_list(args: argparse.Namespace) -> int:
    from eks_harness.plugins import find_tree

    tree = find_tree(Path(args.tree or ".").resolve()) or Path(args.tree or ".").resolve()
    rows = []
    for flows_dir in sorted(tree.rglob(".harness/flows")):
        if any(part in ("node_modules", ".git") for part in flows_dir.parts):
            continue
        for file in sorted(flows_dir.glob("*.py")):
            rows.append({"app": str(flows_dir.parent.parent.relative_to(tree)) or ".", "flow": file.name,
                         "path": str(file)})
    if args.json:
        print_json({"tree": str(tree), "items": rows})
        return 0
    console.print(table(["app", "flow", "path"], [(r["app"], r["flow"], r["path"]) for r in rows]))
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("flow", help="run Python flows against a leased browser or device",
                                   description="A flow is a Python file with flow(app) under <app>/.harness/flows/. "
                                               "It drives the app through the driver worker of a lease and reports "
                                               "checks, screenshots, videos and contact sheets.")
    sub = parser.add_subparsers(dest="flow_command", metavar="<subcommand>", required=True)
    run = sub.add_parser("run", help="run a flow file or inline code")
    run.add_argument("flow", nargs="?", help="path to a flow .py with flow(app)")
    run.add_argument("-e", "--code", help="inline Python with `app` in scope; set `result` to return a value")
    run.add_argument("--platform", choices=["web", "ios", "android"], help="default: the app's first platform")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="values for app.params")
    run.add_argument("--session", help="session name (default: the git branch)")
    run.add_argument("--instance", help="harness instance key (default: the Claude session or the work tree)")
    run.add_argument("--wait", type=float, default=600, help="seconds to wait for a free browser or device")
    run.add_argument("--release", action="store_true", help="release the lease and stop the worker afterwards")
    run.add_argument("--no-fetch", action="store_true", help="do not download captures")
    run.add_argument("--out", help="directory for downloaded captures")
    run.add_argument("-v", "--verbose", action="store_true", help="print every step as it runs")
    run.add_argument("--json", action="store_true", help="print the report as JSON")
    run.set_defaults(func=cmd_run)
    listing = sub.add_parser("list", help="flows in this work tree")
    listing.add_argument("--tree")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

