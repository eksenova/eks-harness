from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from eks_harness.cli.client import (
    ApiClientError,
    DaemonUnavailable,
    HarnessClient,
    client_from_args,
)
from eks_harness.cli.ui import (
    EXIT_USAGE,
    CliExit,
    error,
    info,
    print_json,
    table,
    warn,
    write_line,
)
from eks_harness.engine.model import (
    ControlRequest,
    ControlResponse,
    DoResult,
    JournalPage,
    NodeEventRecord,
    PauseInfo,
    RunComparison,
    RunPage,
    RunReport,
    RunSummary,
)

RUN_FAILED = 1
RUN_ABORTED = 13
RUN_PAUSED = 12
PLAN_INVALID = 1
FOLLOW_TIMEOUT = 3600.0
PROMPT = "eks debug> "
DEBUG_COMMANDS = ("continue", "step", "retry", "skip", "abort", "edit", "do", "break", "delete", "breaks", "status",
                  "help")
CACHE_MODES = ("use", "refresh", "readonly", "off")
SEED_MODES = ("restore", "fresh", "rebuild", "skip")
CONTROL_ACTIONS = ("continue", "step", "retry", "skip", "abort", "edit", "do", "pause", "break_add", "break_remove",
                   "break_list", "status")


def register(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser(
        "run", help="run a flow, seed, edit, scene or score on the hub",
        description="Build the plan of an entry locally, validate it, start it on the hub and follow it until it "
                    "ends. Exit 0 when it passes, 1 when it fails or the plan is invalid, 13 when it is aborted.")
    run.add_argument("spec", help="file.py or file.py::name")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="a parameter of the entry function, parsed into its annotated type (repeatable)")
    run.add_argument("--platform", choices=["web", "ios", "android"], help="the platform of a flow")
    run.add_argument("--backend", help="backend lease id, backend id or definition for the plan's backends")
    run.add_argument("--backend-tree", help="tree the backend runs from")
    run.add_argument("--backend-key", help="identity of the backend instance instead of the tree")
    run.add_argument("--project", help="project owner/name (default: the tree's)")
    run.add_argument("--session", help="session (default: the tree's git branch)")
    run.add_argument("--break", dest="breaks", action="append", default=[], metavar="SPEC",
                     help="pause at a breakpoint: n12, node:n12, op:ui.tap, op:ui.*, label:before-submit, "
                          "when:EXPR, or several joined with commas (repeatable; implies --debug)")
    run.add_argument("--break-on-error", action="store_true", help="pause at a failing node (implies --debug)")
    run.add_argument("--debug", action="store_true", help="let breakpoints pause the run and open the debugger")
    run.add_argument("--repeat", type=int, default=1, metavar="N",
                     help="run N times and report the nodes whose results differ")
    run.add_argument("--cache", choices=CACHE_MODES, default="use", help="cache mode for pure nodes")
    run.add_argument("--seed", dest="seed_mode", choices=SEED_MODES, default="restore",
                     help="restore the seed snapshot unless the backend holds it, restore it always (fresh), build "
                          "it again (rebuild) or use the backend as it is (skip)")
    run.add_argument("--fake", action="append", default=[], metavar="FAMILY",
                     help="simulate ops of this family (or op) with fake executors; * for every op without a real "
                          "executor (repeatable)")
    run.add_argument("--no-plugins", action="store_true", help="do not import the tree's plugin modules")
    run.add_argument("--no-wait", action="store_true", help="start the run and print its id without following it")
    run.add_argument("--timeout", type=float, default=FOLLOW_TIMEOUT, help="seconds to follow the run")
    run.add_argument("--json", action="store_true", help="print the run report as JSON")
    run.set_defaults(func=cmd_run)

    runs = subparsers.add_parser("runs", help="list, show, replay, control and cancel runs",
                                 description="Runs on the hub: their status, reports, journals and debugger.")
    sub = runs.add_subparsers(dest="runs_command", metavar="<command>")
    listing = sub.add_parser("list", help="runs, newest first")
    listing.add_argument("--project", help="only runs of this project")
    listing.add_argument("--session", help="only runs of this session")
    listing.add_argument("--status", action="append", default=[], help="only runs in this state (repeatable)")
    listing.add_argument("--limit", type=int, default=20, help="how many")
    listing.add_argument("--json", action="store_true", help="print JSON")
    listing.set_defaults(func=cmd_runs_list)
    show = sub.add_parser("show", help="a run's report")
    show.add_argument("run", help="run id")
    show.add_argument("--journal", action="store_true", help="also print the node journal")
    show.add_argument("--events", action="store_true", help="also print the events")
    show.add_argument("--json", action="store_true", help="print JSON")
    show.set_defaults(func=cmd_runs_show)
    replay = sub.add_parser("replay", help="run the same plan again")
    replay.add_argument("run", help="run id")
    replay.add_argument("--no-wait", action="store_true", help="do not follow the new run")
    replay.add_argument("--json", action="store_true", help="print the report as JSON")
    replay.set_defaults(func=cmd_runs_replay)
    control = sub.add_parser("control", help="send a debugger action to a run")
    control.add_argument("run", help="run id")
    control.add_argument("action", choices=CONTROL_ACTIONS, help="debugger action")
    control.add_argument("--node", help="node the action targets")
    control.add_argument("--args", help="JSON arguments for edit or do")
    control.add_argument("--op", help="op for do")
    control.add_argument("--value", help="JSON output value for skip")
    control.add_argument("--breakpoint", help="breakpoint spec for break_add")
    control.add_argument("--id", help="breakpoint id for break_remove")
    control.add_argument("--json", action="store_true", help="print JSON")
    control.set_defaults(func=cmd_runs_control)
    cancel = sub.add_parser("cancel", help="abort a run")
    cancel.add_argument("run", help="run id")
    cancel.add_argument("--reason", help="why")
    cancel.add_argument("--json", action="store_true", help="print JSON")
    cancel.set_defaults(func=cmd_runs_cancel)
    runs.set_defaults(func=cmd_runs_help, runs_parser=runs)

    debug = subparsers.add_parser("debug", help="attach the debugger to a run",
                                  description="Follow a run and open the interactive debugger whenever it pauses.")
    debug.add_argument("run", help="run id")
    debug.set_defaults(func=cmd_debug)

    do = subparsers.add_parser("do", help="run one op on the hub, acquiring what it needs",
                               description="Run a single op as a one-node plan and print its value. Arguments are "
                                           "KEY=VALUE pairs (values parsed as JSON when they parse, else text) or "
                                           "--args JSON.")
    do.add_argument("op", help="op name such as api.get or ui.tap")
    do.add_argument("pairs", nargs="*", metavar="KEY=VALUE", help="op arguments")
    do.add_argument("--args", help="op arguments as one JSON object")
    do.add_argument("--platform", choices=["web", "ios", "android"], help="platform for ops that use a lease")
    do.add_argument("--app", help="app profile for ops that use a lease")
    do.add_argument("--persona", help="persona to act as first")
    do.add_argument("--seed", help="seed scenario of the backend")
    do.add_argument("--backend", help="backend lease id, backend id or definition")
    do.add_argument("--project", help="project owner/name")
    do.add_argument("--session", help="session")
    do.add_argument("--fake", action="append", default=[], metavar="FAMILY", help="simulate ops of this family")
    do.add_argument("--wait", type=float, default=120.0, help="seconds to wait")
    do.add_argument("--json", action="store_true", help="print JSON")
    do.set_defaults(func=cmd_do)


def cmd_run(args: argparse.Namespace) -> int:
    from eks_harness.cli.plan_cmds import _build, _entry

    if args.repeat < 1:
        raise CliExit(EXIT_USAGE, "--repeat needs a count of at least 1")
    cwd = Path.cwd()
    plan, validation = _build(args, cwd)
    if not validation.ok:
        for line in validation.text().splitlines():
            write_line(line)
        count = len(validation.errors)
        error(f"{plan.kind} {plan.name} has {count} validation error{'' if count == 1 else 's'}; nothing ran")
        return PLAN_INVALID
    tree = _entry(args.spec, cwd).tree
    body = _run_body(args, json.loads(plan.to_json(indent=None)), tree, cwd)
    with client_from_args(args) as client:
        if args.repeat == 1:
            return _start_and_follow(client, body, args)
        return _repeat(client, body, args)


def _run_body(args: argparse.Namespace, plan: dict[str, Any], tree: Path | None, cwd: Path) -> dict[str, Any]:
    from eks.planning import parse_sets
    from eks_harness.mcp_context import harness_instance

    debug = bool(args.debug or args.breaks or args.break_on_error)
    body: dict[str, Any] = {
        "plan": plan,
        "tree": str(tree) if tree is not None else None,
        "cwd": str(cwd),
        "params": parse_sets(args.set),
        "platform": args.platform,
        "project": args.project,
        "session": args.session,
        "instance": harness_instance(str(tree) if tree is not None else None),
        "options": {"debug": debug, "breakpoints": list(args.breaks), "breakOnError": bool(args.break_on_error),
                    "cache": args.cache, "seed": args.seed_mode, "plugins": not args.no_plugins},
    }
    if args.backend:
        body["backend"] = {"ref": args.backend, "tree": args.backend_tree, "key": args.backend_key}
    if args.fake:
        body["fakes"] = {"families": list(args.fake)}
    return {key: value for key, value in body.items() if value is not None}


def _create(client: HarnessClient, body: Mapping[str, Any]) -> RunSummary:
    created = client.post("/api/runs", json=dict(body))
    for issue in created.get("issues", []):
        where = f" ({issue['src']})" if issue.get("src") else ""
        warn(f"{issue.get('code')}: {issue.get('message')}{where}")
    return RunSummary.model_validate(created["run"])


def _start_and_follow(client: HarnessClient, body: Mapping[str, Any], args: argparse.Namespace) -> int:
    started = _create(client, body)
    if getattr(args, "no_wait", False):
        if args.json:
            print_json(started.model_dump(mode="json"))
        else:
            write_line(started.id)
        return 0
    return _watch(client, started.id, args, timeout=getattr(args, "timeout", FOLLOW_TIMEOUT))


def _repeat(client: HarnessClient, body: Mapping[str, Any], args: argparse.Namespace) -> int:
    first: RunSummary | None = None
    codes: list[int] = []
    reports: list[RunReport] = []
    comparisons: list[RunComparison] = []
    for index in range(args.repeat):
        attempt = dict(body)
        if first is not None:
            attempt["repeatOf"] = first.id
        started = _create(client, attempt)
        if not args.json:
            info(f"run {index + 1}/{args.repeat}: {started.id}")
        quiet = argparse.Namespace(**{**vars(args), "json": True}) if not args.json else args
        code, report = _follow_to_end(client, started.id, quiet, timeout=args.timeout, render=False)
        codes.append(code)
        reports.append(report)
        if first is None:
            first = started
            continue
        comparison = RunComparison.model_validate(
            client.get(f"/api/runs/{_q(first.id)}/compare/{_q(started.id)}"))
        comparisons.append(comparison)
    if args.json:
        print_json({"runs": [report.model_dump(mode="json") for report in reports],
                    "comparisons": [comparison.model_dump(mode="json") for comparison in comparisons]})
    else:
        for report in reports:
            write_line(f"{report.run.id} {report.run.status} {report.timings.total_ms} ms")
        _print_comparisons(comparisons)
    return max(codes, key=_severity)


def _severity(code: int) -> int:
    order = {0: 0, RUN_FAILED: 2, RUN_PAUSED: 1, RUN_ABORTED: 3}
    return order.get(code, 4)


def _print_comparisons(comparisons: list[RunComparison]) -> None:
    diverging = [comparison for comparison in comparisons if not comparison.same]
    if not diverging:
        info(f"every repeat matched the first run ({len(comparisons)} comparison{'' if len(comparisons) == 1 else 's'})")
        return
    for comparison in diverging:
        warn(f"{comparison.b} diverges from {comparison.a} in {len(comparison.diverging)} node instance"
             f"{'' if len(comparison.diverging) == 1 else 's'}")
        rows = [(item.node + (f"@{item.instance}" if item.instance else ""), item.op, item.field,
                 _short(item.a), _short(item.b)) for item in comparison.diverging]
        write_table(["node", "op", "field", comparison.a, comparison.b], rows)


def _watch(client: HarnessClient, run_id: str, args: argparse.Namespace, *, timeout: float) -> int:
    code, _report = _follow_to_end(client, run_id, args, timeout=timeout, render=True)
    return code


def _follow_to_end(client: HarnessClient, run_id: str, args: argparse.Namespace, *, timeout: float,
                   render: bool) -> tuple[int, RunReport]:
    printer = _Printer(client, run_id, quiet=bool(args.json))
    interactive = sys.stdin.isatty() and not args.json
    for record in follow(client, run_id, timeout=timeout):
        printer.event(record)
        if record.type != "run.paused":
            continue
        current = _status(client, run_id)
        if current.status != "paused" or current.pause is None:
            continue
        if not interactive:
            warn(f"run {run_id} paused ({_pause_line(current.pause)}); attach with: eks-harness debug {run_id}")
            report = _report(client, run_id)
            if args.json and render:
                print_json(report.model_dump(mode="json"))
            return RUN_PAUSED, report
        if not debugger_prompt(client, run_id, current.pause.model_dump(mode="json")):
            info(f"detached; attach again with: eks-harness debug {run_id}")
            return RUN_PAUSED, _report(client, run_id)
    report = _report(client, run_id)
    if render:
        if args.json:
            print_json(report.model_dump(mode="json"))
        else:
            render_report(report)
    if report.run.status not in ("passed", "failed", "aborted", "error", "paused"):
        warn(f"run {run_id} is still {report.run.status} after {timeout:g} s; follow it with: eks-harness debug "
             f"{run_id}")
    return exit_code(report.run), report


def _status(client: HarnessClient, run_id: str) -> RunSummary:
    return RunSummary.model_validate(client.get(f"/api/runs/{_q(run_id)}"))


def _report(client: HarnessClient, run_id: str) -> RunReport:
    return RunReport.model_validate(client.get(f"/api/runs/{_q(run_id)}/report"))


@dataclass
class _Printer:
    client: HarnessClient
    run_id: str
    quiet: bool
    nodes: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    started: dict[tuple[str, str], float] = field(default_factory=dict)
    cached: dict[tuple[str, str], str] = field(default_factory=dict)
    loaded: bool = False

    def event(self, record: NodeEventRecord) -> None:
        if self.quiet:
            return
        if record.type.startswith("run.") and record.node_id is None:
            self._run_event(record)
            return
        if record.node_id is None:
            return
        key = (record.node_id, record.instance)
        if record.type == "node.started":
            self.started[key] = record.ts
            return
        if record.type == "node.cache":
            outcome = record.detail.get("outcome")
            if isinstance(outcome, str):
                self.cached[key] = outcome
            return
        status = _NODE_OUTCOMES.get(record.type)
        if status is None:
            return
        op, src = self._node(record.node_id)
        began = self.started.get(key)
        ms = f"{int((record.ts - began) * 1000)} ms" if began is not None else ""
        cache = " (cache hit)" if self.cached.get(key) == "hit" else ""
        node = record.node_id + (f"@{record.instance}" if record.instance else "")
        reason = ""
        message = record.detail.get("message")
        if status in ("failed", "soft_failed") and isinstance(message, str):
            reason = f": {message}"
        write_line(" ".join(part for part in (status, op, node, src or "", ms) if part) + cache + reason)

    def _run_event(self, record: NodeEventRecord) -> None:
        status = record.type.removeprefix("run.")
        if status in ("queued", "outputs_unresolved", "warning"):
            return
        reason = record.detail.get("reason")
        write_line(f"run {status}" + (f": {reason}" if isinstance(reason, str) and reason else ""))

    def _node(self, node_id: str) -> tuple[str, str | None]:
        if node_id not in self.nodes:
            self._load()
        return self.nodes.get(node_id, ("?", None))

    def _load(self) -> None:
        if not self.loaded:
            self.loaded = True
            try:
                plan = self.client.get(f"/api/runs/{_q(self.run_id)}/plan")
            except ApiClientError:
                plan = None
            if isinstance(plan, dict):
                self._walk(plan.get("nodes") or [])
        try:
            page = JournalPage.model_validate(self.client.get(f"/api/runs/{_q(self.run_id)}/journal"))
        except ApiClientError:
            return
        for node in page.nodes:
            self.nodes.setdefault(node.node_id, (node.op, node.src))

    def _walk(self, nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            if isinstance(node_id, str):
                self.nodes[node_id] = (str(node.get("op") or "?"), node.get("src"))
            branches = node.get("branches") or {}
            if isinstance(branches, dict):
                for children in branches.values():
                    if isinstance(children, list):
                        self._walk(children)


_NODE_OUTCOMES = {"node.passed": "passed", "node.soft_failed": "soft_failed", "node.failed": "failed",
                  "node.skipped": "skipped", "node.cancelled": "cancelled"}


def cmd_runs_help(args: argparse.Namespace) -> int:
    args.runs_parser.print_help(sys.stderr)
    return EXIT_USAGE


def cmd_runs_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        params: dict[str, Any] = {"project": args.project, "session": args.session, "limit": args.limit}
        if args.status:
            params["status"] = list(args.status)
        page = RunPage.model_validate(client.get("/api/runs", params=params))
    if args.json:
        print_json(page.model_dump(mode="json"))
        return 0
    if not page.items:
        info("no runs")
        return 0
    rows = [(run.id, run.status, f"{run.plan.kind} {run.plan.name}", run.project or "", run.session or "",
             _when(run.created_at), _counts(run.counts)) for run in page.items]
    write_table(["id", "status", "plan", "project", "session", "created", "nodes"], rows)
    return 0


def cmd_runs_show(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        report = _report(client, args.run)
        page: JournalPage | None = None
        events: list[NodeEventRecord] = []
        if args.journal:
            page = JournalPage.model_validate(client.get(f"/api/runs/{_q(args.run)}/journal"))
        if args.events:
            events = _all_events(client, args.run)
    if args.json:
        payload: dict[str, Any] = {"report": report.model_dump(mode="json")}
        if page is not None:
            payload["journal"] = page.model_dump(mode="json")
        if args.events:
            payload["events"] = [event.model_dump(mode="json") for event in events]
        print_json(payload)
        return exit_code(report.run)
    render_report(report)
    if page is not None:
        rows = [(node.seq, node.status, node.op, node.node_id + (f"@{node.instance}" if node.instance else ""),
                 node.src or "", node.cache or "", node.attempt) for node in page.nodes]
        write_table(["seq", "status", "op", "node", "src", "cache", "attempts"], rows)
    if args.events:
        rows = [(event.id, _when(event.ts), event.type,
                 (event.node_id or "") + (f"@{event.instance}" if event.instance else ""), _short(event.detail))
                for event in events]
        write_table(["id", "time", "type", "node", "detail"], rows)
    return exit_code(report.run)


def _all_events(client: HarnessClient, run_id: str) -> list[NodeEventRecord]:
    events: list[NodeEventRecord] = []
    after: int | None = None
    while True:
        params: dict[str, Any] = {"events": "true", "after": after}
        page = JournalPage.model_validate(client.get(f"/api/runs/{_q(run_id)}/journal", params=params))
        events.extend(page.events)
        if page.next_after is None or page.next_after == after or not page.events:
            return events
        after = page.next_after


def cmd_runs_replay(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        created = client.post(f"/api/runs/{_q(args.run)}/replay")
        started = RunSummary.model_validate(created["run"])
        if args.no_wait:
            if args.json:
                print_json(started.model_dump(mode="json"))
            else:
                write_line(started.id)
            return 0
        if not args.json:
            info(f"replaying {args.run} as {started.id}")
        return _watch(client, started.id, args, timeout=FOLLOW_TIMEOUT)


def cmd_runs_control(args: argparse.Namespace) -> int:
    request: dict[str, Any] = {"action": args.action, "node": args.node, "op": args.op, "breakpoint": args.breakpoint,
                               "id": args.id}
    if args.args is not None:
        request["args"] = _usage_object(args.args, "--args")
    if args.value is not None:
        request["value"] = _json_value(args.value, "--value")
    with client_from_args(args) as client:
        response = _control(client, args.run, request)
    if args.json:
        print_json(response.model_dump(mode="json"))
    else:
        _print_control(response)
    return 0 if response.ok else 1


def cmd_runs_cancel(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        ended = RunSummary.model_validate(client.post(f"/api/runs/{_q(args.run)}/cancel",
                                                      json={"reason": args.reason}, timeout=60.0))
    if args.json:
        print_json(ended.model_dump(mode="json"))
    else:
        write_line(f"{ended.id} {ended.status}" + (f": {ended.reason}" if ended.reason else ""))
    return exit_code(ended)


def cmd_debug(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        raise CliExit(EXIT_USAGE, "eks-harness debug needs a terminal; use eks-harness runs control without one")
    attach = argparse.Namespace(**{**vars(args), "json": False})
    with client_from_args(args) as client:
        current = _status(client, args.run)
        if current.status in ("passed", "failed", "aborted", "error"):
            render_report(_report(client, args.run))
            return exit_code(current)
        paused = current.status == "paused" and current.pause is not None
        if paused and current.pause is not None and not debugger_prompt(client, args.run,
                                                                         current.pause.model_dump(mode="json")):
            return RUN_PAUSED
        return _watch(client, args.run, attach, timeout=FOLLOW_TIMEOUT)


def cmd_do(args: argparse.Namespace) -> int:
    from eks.discovery import find_tree

    arguments = parse_pairs(list(args.pairs), args.args)
    tree = find_tree(Path.cwd())
    body: dict[str, Any] = {
        "args": arguments, "platform": args.platform, "app": args.app, "persona": args.persona, "seed": args.seed,
        "project": args.project, "session": args.session, "tree": str(tree.resolve()) if tree is not None else None,
        "wait": args.wait,
    }
    if args.backend:
        body["backend"] = {"ref": args.backend}
    if args.fake:
        body["fakes"] = {"families": list(args.fake)}
    with client_from_args(args) as client:
        result = DoResult.model_validate(client.post(f"/api/ops/{quote(args.op, safe='')}/do",
                                                     json={k: v for k, v in body.items() if v is not None},
                                                     timeout=args.wait + 30.0))
    if args.json:
        print_json(result.model_dump(mode="json"))
    else:
        _print_do(args.op, result)
    return 0 if result.run.status == "passed" else 1


def _print_do(op: str, result: DoResult) -> None:
    if result.failure is not None:
        error(f"{op} failed: {result.failure.code}: {result.failure.message}")
    elif result.run.status != "passed":
        warn(f"{op} ended {result.run.status}" + (f": {result.run.reason}" if result.run.reason else ""))
    if result.value is not None:
        print_json(result.value)
    for artifact in result.artifacts:
        write_line(_artifact_line(artifact.kind, artifact.name, artifact.url, artifact.raw_url))
    info(f"run {result.run.id}")


def follow(client: HarnessClient, run_id: str, *, timeout: float,
           last_event_id: int | None = None) -> Iterator[NodeEventRecord]:
    deadline = time.monotonic() + max(timeout, 0.0)
    last = last_event_id
    failures = 0
    path = f"/api/runs/{_q(run_id)}/events"
    while time.monotonic() < deadline:
        headers = {"Last-Event-ID": str(last)} if last is not None else {}
        ended = False
        try:
            with client.stream("GET", path, headers=headers, timeout=_READ_TIMEOUT) as response:
                failures = 0
                frame: dict[str, str] = {}
                data: list[str] = []
                for line in response.iter_lines():
                    if time.monotonic() >= deadline:
                        return
                    if line:
                        if line.startswith(":"):
                            continue
                        name, _sep, value = line.partition(":")
                        value = value.removeprefix(" ")
                        if name == "data":
                            data.append(value)
                        else:
                            frame[name] = value
                        continue
                    kind = frame.get("event")
                    payload = "\n".join(data)
                    frame, data = {}, []
                    if kind == "end":
                        ended = True
                        break
                    if kind == "progress" or not payload:
                        continue
                    record = NodeEventRecord.model_validate_json(payload)
                    if last is not None and record.id <= last:
                        continue
                    last = record.id
                    yield record
        except (httpx.TransportError, DaemonUnavailable):
            failures += 1
            if failures > _RECONNECTS:
                raise
            time.sleep(min(_BACKOFF * failures, _BACKOFF_MAX))
            continue
        if ended:
            return
        time.sleep(_BACKOFF)


_READ_TIMEOUT = 120.0
_RECONNECTS = 10
_BACKOFF = 0.5
_BACKOFF_MAX = 5.0


def exit_code(summary: RunSummary) -> int:
    if summary.status == "passed":
        return 0
    if summary.status == "aborted":
        return RUN_ABORTED
    if summary.status == "paused":
        return RUN_PAUSED
    return RUN_FAILED


def render_report(report: RunReport) -> None:
    run = report.run
    write_line(f"{run.plan.kind} {run.plan.name}: {run.status}" + (f" ({run.reason})" if run.reason else ""))
    write_line(f"run {run.id}" + (", simulated" if run.simulated else "")
               + (f", replay of {run.replay_of}" if run.replay_of else ""))
    for failure in report.failures:
        where = f" at {failure.src}" if failure.src else ""
        head = f"{'soft ' if failure.soft else ''}failure {failure.op or 'run'}"
        line = f"{head} {failure.node or ''}{where}: {failure.code}: {failure.message}"
        (warn if failure.soft else error)(line.replace("  ", " "))
        for check in failure.checks:
            if not check.ok:
                write_line(f"  {check.severity} {check.kind}: {check.message}")
        for shot in failure.shots:
            write_line(f"  {_artifact_line(shot.kind, shot.name, shot.url, shot.raw_url)}")
    if report.checkpoints:
        rows = [(checkpoint.label, checkpoint.viewport or "", "ok" if checkpoint.ok else "failed",
                 ", ".join(f"{check.kind}: {check.message}" for check in checkpoint.checks if not check.ok))
                for checkpoint in report.checkpoints]
        write_table(["checkpoint", "viewport", "result", "problems"], rows)
    if report.artifacts:
        write_line("artifacts:")
        for artifact in report.artifacts:
            write_line(f"  {_artifact_line(artifact.kind, artifact.name, artifact.url, artifact.raw_url)}")
    if report.values is not None:
        write_line("values:")
        write_line(json.dumps(report.values, ensure_ascii=False, indent=2, default=str))
    timings = report.timings
    write_line(f"time: {timings.total_ms} ms (queued {timings.queued_ms}, preparing {timings.preparing_ms}, "
               f"running {timings.running_ms}, paused {timings.paused_ms})")
    if timings.nodes:
        slowest = ", ".join(f"{timing.op} {timing.node} {timing.ms} ms" for timing in timings.nodes[:_SLOWEST])
        write_line(f"slowest: {slowest}")
    cache = report.cache
    if cache.hits or cache.misses or cache.stored or cache.bypassed:
        write_line(f"cache: {cache.hits} hits, {cache.misses} misses, {cache.stored} stored, {cache.bypassed} "
                   f"bypassed")
    if run.links.session:
        write_line(f"session: {run.links.session}")


_SLOWEST = 5


def _artifact_line(kind: str, name: str | None, url: str | None, raw: str | None) -> str:
    label = f"{kind} {name}" if name else kind
    return " ".join(part for part in (label, url or "", raw or "") if part)


def parse_pairs(pairs: list[str], raw: str | None) -> dict[str, Any]:
    values: dict[str, Any] = _usage_object(raw, "--args") if raw is not None else {}
    for pair in pairs:
        key, sep, text = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"{pair!r} is not KEY=VALUE")
        try:
            values[key.strip()] = json.loads(text)
        except json.JSONDecodeError:
            values[key.strip()] = text
    return values


def _json_object(text: str, what: str) -> dict[str, Any]:
    value = _json_value(text, what)
    if not isinstance(value, dict):
        raise TypeError(f"{what} must be a JSON object")
    return value


def _usage_object(text: str, what: str) -> dict[str, Any]:
    try:
        return _json_object(text, what)
    except TypeError as problem:
        raise CliExit(EXIT_USAGE, str(problem)) from None


def _json_value(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as problem:
        raise ValueError(f"{what} is not valid JSON: {problem.msg} at column {problem.colno}") from None


def debugger_prompt(client: HarnessClient, run_id: str, pause: Mapping[str, Any]) -> bool:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter

    _print_pause(PauseInfo.model_validate(pause))
    session: PromptSession[str] = PromptSession(completer=WordCompleter(list(DEBUG_COMMANDS)))
    while True:
        try:
            line = session.prompt(PROMPT).strip()
        except EOFError:
            return False
        except KeyboardInterrupt:
            continue
        if not line:
            continue
        word, _sep, rest = line.partition(" ")
        rest = rest.strip()
        try:
            request = _command(word, rest)
        except (ValueError, TypeError) as problem:
            error(str(problem))
            continue
        if request is None:
            continue
        try:
            response = _control(client, run_id, request)
        except ApiClientError as problem:
            error(problem.message)
            if problem.status == 409:
                return True
            continue
        _print_control(response)
        if request["action"] in _RESUMING and response.ok:
            return True


_RESUMING = frozenset({"continue", "step", "retry", "skip", "abort", "edit"})


def _command(word: str, rest: str) -> dict[str, Any] | None:
    if word == "help":
        write_line("continue | step | retry | skip [json value] | abort | edit <json args> | do <op> [json args] | "
                   "break <spec> | delete <id> | breaks | status | help; Ctrl-D detaches")
        return None
    if word in ("continue", "step", "retry", "abort", "status"):
        return {"action": word}
    if word == "skip":
        return {"action": "skip", **({"value": _json_value(rest, "skip value")} if rest else {})}
    if word == "edit":
        if not rest:
            raise ValueError("edit needs the new arguments as a JSON object")
        return {"action": "edit", "args": _json_object(rest, "edit arguments")}
    if word == "do":
        op, _sep, raw = rest.partition(" ")
        if not op:
            raise ValueError("do needs an op name, such as: do ui.text {\"target\": \"#total\"}")
        return {"action": "do", "op": op, **({"args": _json_object(raw.strip(), "do arguments")} if raw.strip()
                                              else {})}
    if word == "break":
        if not rest:
            raise ValueError("break needs a spec such as n12, op:ui.tap or label:before-submit")
        return {"action": "break_add", "breakpoint": rest}
    if word == "delete":
        if not rest:
            raise ValueError("delete needs a breakpoint id (see breaks)")
        return {"action": "break_remove", "id": rest}
    if word == "breaks":
        return {"action": "break_list"}
    raise ValueError(f"unknown command {word!r}; type help")


def _control(client: HarnessClient, run_id: str, request: Mapping[str, Any]) -> ControlResponse:
    payload = ControlRequest.model_validate({key: value for key, value in request.items() if value is not None})
    return ControlResponse.model_validate(client.post(f"/api/runs/{_q(run_id)}/control",
                                                      json=payload.model_dump(mode="json", exclude_none=True)))


def _print_control(response: ControlResponse) -> None:
    (info if response.ok else warn)(response.message or f"{'ok' if response.ok else 'not applied'}: "
                                                         f"{response.status}, debugger {response.debugger}")
    if response.result is not None:
        print_json(response.result)
    if response.breakpoints:
        rows = [(point.id, point.text, "on" if point.enabled else "off", point.hits) for point in response.breakpoints]
        write_table(["id", "breakpoint", "state", "hits"], rows)
    if response.pause is not None and response.status == "paused":
        _print_pause(response.pause)


def _print_pause(pause: PauseInfo) -> None:
    warn(f"paused: {_pause_line(pause)}")
    if pause.error is not None:
        error(f"  {pause.error.code}: {pause.error.message}")
    if pause.args is not None:
        write_line(f"  args: {_short(pause.args, _ARGS_WIDTH)}")
    for snapshot in pause.snapshots:
        write_line(f"  {_artifact_line(snapshot.kind, snapshot.name, snapshot.url, snapshot.raw_url)}")


def _pause_line(pause: PauseInfo) -> str:
    parts = [pause.reason]
    if pause.op:
        parts.append(pause.op)
    if pause.node:
        parts.append(pause.node + (f"@{pause.instance}" if pause.instance else ""))
    if pause.label:
        parts.append(f"[{pause.label}]")
    if pause.src:
        parts.append(f"at {pause.src}")
    if pause.breakpoint:
        parts.append(f"breakpoint {pause.breakpoint}")
    return " ".join(parts)


_ARGS_WIDTH = 200
_DETAIL_WIDTH = 80


def _short(value: Any, width: int = _DETAIL_WIDTH) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= width else text[:width - 3] + "..."


def _counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{count} {status}" for status, count in sorted(counts.items()) if count)


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def _q(value: str) -> str:
    return quote(value, safe="")


def write_table(columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    from eks_harness.cli.ops_cmds import plain_rows, wide_console

    wide_console().print(table(columns, plain_rows(rows)))


__all__ = [
    "CACHE_MODES",
    "CONTROL_ACTIONS",
    "DEBUG_COMMANDS",
    "FOLLOW_TIMEOUT",
    "PLAN_INVALID",
    "PROMPT",
    "RUN_ABORTED",
    "RUN_FAILED",
    "RUN_PAUSED",
    "cmd_debug",
    "cmd_do",
    "cmd_run",
    "cmd_runs_cancel",
    "cmd_runs_control",
    "cmd_runs_help",
    "cmd_runs_list",
    "cmd_runs_replay",
    "cmd_runs_show",
    "debugger_prompt",
    "exit_code",
    "follow",
    "parse_pairs",
    "register",
    "render_report",
]
