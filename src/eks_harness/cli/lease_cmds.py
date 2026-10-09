from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from eks_harness.cli.client import DaemonUnavailable, HarnessClient, client_from_args
from eks_harness.cli.ui import (
    EXIT_ERROR,
    EXIT_USAGE,
    console,
    fail,
    info,
    kv_panel,
    ok,
    print_json,
    table,
    warn,
)
from eks_harness.pools.backends import pid_started

KINDS = ("browser", "chrome", "ios", "android")
POLL_SLICE = 25.0
DEFAULT_WAIT = 1800.0


def git(tree: Path | str, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=tree, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_tree(value: str | None) -> Path | None:
    for candidate in (value, os.environ.get("EKS_TREE"), os.getcwd()):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.exists():
            if candidate == value:
                fail(f"no such directory: {path}", EXIT_USAGE)
            continue
        top = git(path.resolve(), "rev-parse", "--show-toplevel")
        if top:
            return Path(top).resolve()
        if candidate == value:
            fail(f"not a git work tree: {path}", EXIT_USAGE)
    return None


def branch_of(tree: Path | None) -> str | None:
    if tree is None:
        return None
    branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        short = git(tree, "rev-parse", "--short", "HEAD")
        return f"detached-{short}" if short else None
    return branch


def instance_key(explicit: str | None, tree: Path | None) -> str:
    if explicit:
        return explicit
    forced = os.environ.get("EKS_HARNESS_INSTANCE")
    if forced:
        return forced
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        return f"session:{session}"
    if tree is None:
        fail("no harness instance: pass --instance, set EKS_HARNESS_INSTANCE, or run inside a git work tree",
             EXIT_USAGE)
    return f"tree:{tree}"


def owner_process(args: argparse.Namespace) -> tuple[int | None, str | None]:
    if getattr(args, "owner_pid", None):
        return int(args.owner_pid), pid_started(int(args.owner_pid)) or None
    raw = os.environ.get("CLAUDE_PID", "")
    if raw.isdigit() and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return int(raw), pid_started(int(raw)) or None
    return None, None


def normalize_kind(kind: str | None) -> str | None:
    return "browser" if kind == "chrome" else kind


def shell_line(name: str, value: Any) -> str:
    return f"{name}={shlex.quote('' if value is None else str(value))}"


def lease_exports(result: dict) -> dict[str, Any]:
    lease = result.get("lease") or {}
    urls = result.get("urls") or lease.get("urls") or {}
    session = result.get("session") or {}
    project = result.get("project") or {}
    exports: dict[str, Any] = {
        "EKS_SID": lease.get("sid"), "EKS_LEASE_SID": lease.get("sid"), "EKS_LEASE_ID": lease.get("id"),
        "EKS_LEASE_KIND": lease.get("kind"), "EKS_LEASE_RESOURCE": lease.get("resource"),
        "EKS_PROJECT": project.get("id") or lease.get("projectId"),
        "EKS_SESSION": session.get("name") or lease.get("sessionName"),
        "EKS_SESSION_SLUG": session.get("slug") or lease.get("sessionSlug"),
        "EKS_SESSION_URL": urls.get("session"), "EKS_UI_URL": urls.get("ui"),
    }
    if lease.get("kind") == "browser":
        exports["EKS_CDP_URL"] = lease.get("cdp") or ""
    device = lease.get("device") or {}
    if device.get("udid"):
        exports.update({"EKS_DEVICE_UDID": device["udid"], "EKS_DEVICE_NAME": device.get("name")})
    if device.get("serial"):
        exports.update({"EKS_DEVICE_SERIAL": device["serial"], "EKS_DEVICE_NAME": device.get("name")})
    return {k: v for k, v in exports.items() if v is not None}


def print_lease_result(result: dict, args: argparse.Namespace) -> None:
    if getattr(args, "shell", False):
        for name, value in lease_exports(result).items():
            print(shell_line(name, value))
        return
    if args.json:
        print_json(result)
        return
    lease = result.get("lease") or {}
    urls = result.get("urls") or {}
    ok(f"{lease.get('kind')} {lease.get('resource')} leased: sid {lease.get('sid')}")
    sys.stdout.write(f"{lease.get('sid')}\n")
    for key in ("session", "ui"):
        if urls.get(key):
            sys.stdout.write(f"{urls[key]}\n")
    sys.stdout.flush()


def wait_for_lease(client: HarnessClient, path: str, body: dict, wait: float, kind: str) -> dict:
    deadline = time.time() + wait
    last = ""
    while True:
        remaining = max(0.0, deadline - time.time())
        slice_seconds = min(POLL_SLICE, remaining)
        result = client.post(path, json={**body, "wait": wait > 0}, params={"waitSeconds": round(slice_seconds, 1)},
                             timeout=slice_seconds + 30)
        status = result.get("status")
        if status == "granted":
            return result
        if status == "failed":
            lease = result.get("lease") or {}
            fail(f"{kind} could not be prepared: {result.get('error') or lease.get('error') or lease.get('reason')}",
                 EXIT_ERROR)
        if status == "queued":
            holders = ", ".join(f"{h.get('resource')}={str(h.get('instance'))[-12:]} ({h.get('idleSeconds')}s idle)"
                                for h in result.get("holders") or [])
            message = (f"{kind} is full; waiting in the queue {result.get('position')}/{result.get('waiting')}. "
                       f"Held by: {holders or '-'}")
        else:
            message = f"{kind} is being prepared ({result.get('resource')})"
        signature = f"{status}:{result.get('position')}:{[h.get('instance') for h in result.get('holders') or []]}"
        if signature != last:
            info(message)
            last = signature
        if time.time() >= deadline:
            if wait <= 0:
                return result
            fail(f"no {kind} within {wait:.0f}s: {message}", EXIT_ERROR)
        body = {**body}
        if path.endswith("/resume") and result.get("sid"):
            body["sid"] = result["sid"]


def acquire_body(args: argparse.Namespace) -> dict:
    tree = resolve_tree(args.tree)
    session = args.session or branch_of(tree)
    if not session:
        fail("--session is required (no git branch to default to)", EXIT_USAGE)
    owner_pid, owner_started = owner_process(args)
    instance = instance_key(args.instance, tree)
    return {"kind": normalize_kind(args.kind), "project": args.project, "session": session, "instance": instance,
            "backend": args.backend, "owner_pid": owner_pid, "owner_started": owner_started,
            "tree": str(tree) if tree else None, "state_dir": args.state_dir,
            "label": args.label or (f"{Path(str(tree)).name}" if tree else None)}


def cmd_acquire(args: argparse.Namespace) -> int:
    body = acquire_body(args)
    with client_from_args(args) as client:
        wait = 0.0 if args.no_wait else float(args.wait)
        result = wait_for_lease(client, "/api/leases/acquire", body, wait, body["kind"])
    if result.get("status") == "queued":
        if args.json:
            print_json(result)
        else:
            info(f"queued at position {result.get('position')} of {result.get('waiting')} (sid {result.get('sid')})")
            sys.stdout.write(f"{result.get('sid')}\n")
        return 0
    print_lease_result(result, args)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.tree) if args.tree else None
    owner_pid, owner_started = owner_process(args)
    body = {"sid": args.sid, "instance": args.instance or (instance_key(None, tree) if (
        tree or os.environ.get("EKS_HARNESS_INSTANCE") or os.environ.get("CLAUDE_CODE_SESSION_ID")) else None),
        "backend": args.backend, "owner_pid": owner_pid, "owner_started": owner_started,
        "tree": str(tree) if tree else None, "state_dir": args.state_dir}
    with client_from_args(args) as client:
        wait = 0.0 if args.no_wait else float(args.wait)
        result = wait_for_lease(client, "/api/leases/resume", body, wait, "lease")
    print_lease_result(result, args)
    return 0


def target_instance(args: argparse.Namespace) -> str:
    return instance_key(args.instance, resolve_tree(args.tree))


def cmd_heartbeat(args: argparse.Namespace) -> int:
    try:
        with client_from_args(args) as client:
            if args.sid:
                result = client.post(f"/api/leases/{quote(args.sid)}/heartbeat", json={}, timeout=10)
            else:
                instance = target_instance(args)
                result = client.post(f"/api/instances/{quote(instance, safe='')}/heartbeat",
                                     json={"kind": normalize_kind(args.kind)}, timeout=10)
    except DaemonUnavailable:
        return 0
    if args.json:
        print_json(result)
    return 0


def cmd_idle(args: argparse.Namespace) -> int:
    body = {"grace": args.grace} if args.grace is not None else {}
    try:
        with client_from_args(args) as client:
            if args.sid:
                result = client.post(f"/api/leases/{quote(args.sid)}/idle", json=body, timeout=10)
            else:
                instance = target_instance(args)
                result = client.post(f"/api/instances/{quote(instance, safe='')}/idle", json=body, timeout=10)
    except DaemonUnavailable:
        return 0
    if args.json:
        print_json(result)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        if args.sid:
            result = client.post(f"/api/leases/{quote(args.sid)}/release", json={"reason": args.reason}, timeout=300)
        else:
            instance = target_instance(args)
            result = client.post(f"/api/instances/{quote(instance, safe='')}/release",
                                 json={"kind": normalize_kind(args.kind), "reason": args.reason or "requested"},
                                 timeout=300)
    if args.json:
        print_json(result)
    else:
        ok(f"released: {', '.join(result.get('released') or []) or 'no lease held'}")
    return 0


def cmd_break(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        result = client.post("/api/leases/break", json={"sid": args.sid, "reason": args.reason or "broken by hand"},
                             timeout=300)
    if args.json:
        print_json(result)
    else:
        ok(f"broken: {', '.join(result.get('sids') or []) or 'nothing (already ended)'}")
    return 0


def cmd_ended(args: argparse.Namespace) -> int:
    instance = target_instance(args)
    try:
        with client_from_args(args) as client:
            result = client.post(f"/api/instances/{quote(instance, safe='')}/ended",
                                 json={"reason": args.reason}, timeout=600)
    except DaemonUnavailable:
        if not args.json:
            info("the daemon is not running: nothing to release")
        return 0
    if args.json:
        print_json(result)
    else:
        ok(f"instance {instance} ended; released: {', '.join(result.get('released') or []) or 'nothing'}")
    return 0


def idle_text(seconds: Any) -> str:
    if seconds is None:
        return "-"
    value = float(seconds)
    return f"{value:.0f}s" if value < 120 else f"{value / 60:.0f} min"


def render_leases(items: list[dict], title: str, mine: str | None = None) -> None:
    rows = []
    for lease in items:
        mark = " (this instance)" if mine and lease.get("ownerInstance") == mine else ""
        session = f"{lease.get('projectId') or '-'} / {lease.get('sessionName') or '-'}"
        state = lease.get("state") or ""
        if lease.get("phase") and lease.get("phase") != "ready":
            state += f" ({lease['phase']})"
        rows.append((lease.get("sid"), lease.get("kind"), lease.get("resource") or "-", session,
                     str(lease.get("ownerInstance") or "")[-28:] + mark, state, idle_text(lease.get("idleSeconds")),
                     lease.get("stale") or lease.get("reason") or "-"))
    console.print(table(("sid", "kind", "resource", "project / session", "instance", "state", "idle", "note"),
                        rows, title=title))


def render_status(status: dict, mine: str | None) -> None:
    rows = [("URL", status.get("url")), ("Public URL", status.get("publicUrl")), ("PID", status.get("pid")),
            ("Version", f"{status.get('version')} ({(status.get('sourceHash') or '')[:12]})"),
            ("Mode", ("service" if status.get("managed") else "on demand") + (", fake pools" if status.get(
                "fakePools") else "")), ("Auth", "on" if status.get("authEnabled") else "off"),
            ("Uptime", idle_text(status.get("uptimeSeconds")))]
    if status.get("restartPending"):
        rows.append(("Restart", "pending: some settings need a restart"))
    descriptors = status.get("fileDescriptors") or {}
    if descriptors:
        rows.append(("Open files", f"{descriptors.get('open')} of {descriptors.get('limit') or 'unlimited'}, "
                                   f"{descriptors.get('dbConnections')} database connections"
                     + (" (near the limit)" if descriptors.get("pressure") else "")))
    console.print(kv_panel("eks-harness daemon", rows))
    if descriptors.get("pressure"):
        warn(f"the daemon has {descriptors.get('open')} files open of its limit of {descriptors.get('limit')}; "
             f"it fails once it reaches the limit")
    render_leases(status.get("leases") or [], "Leases", mine)
    queue = status.get("queue") or []
    if queue:
        render_leases(queue, "Queue", mine)
    pool_rows = []
    for browser in status.get("browsers") or []:
        pool_rows.append((f"browser:{browser.get('index')}", Path(browser.get("binary") or "").name or "-",
                          browser.get("statusText"), browser.get("cdp") or "-"))
    for device in status.get("devices") or []:
        pool_rows.append((device.get("key"), device.get("name"), device.get("statusText"),
                          device.get("udid") or device.get("serial") or "-"))
    console.print(table(("resource", "name", "status", "id"), pool_rows, title="Browsers and devices"))
    backends = status.get("backends") or []
    if backends:
        console.print(table(("id", "status", "ports", "bound to"), [
            (b.get("id", "")[-40:], b.get("status"), ", ".join(f"{k}={v}" for k, v in (b.get("ports") or {}).items()),
             ", ".join(b.get("bindings") or []) or "-") for b in backends], title="Backends"))


def cmd_status(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        status = client.get("/api/status")
    if args.json:
        print_json(status)
        return 0
    mine = None
    try:
        mine = instance_key(args.instance, resolve_tree(args.tree))
    except Exception:
        mine = None
    render_status(status, mine)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"kind": normalize_kind(args.kind), "instance": args.instance, "project": args.project,
                              "session": args.session, "includeEnded": "true" if args.ended else None}
    with client_from_args(args) as client:
        result = client.get("/api/leases", params=params)
    if args.json:
        print_json(result)
        return 0
    render_leases(result.get("items") or [], "Leases")
    if result.get("queue"):
        render_leases(result["queue"], "Queue")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        lease = client.get(f"/api/leases/{quote(args.sid)}")
    if args.json:
        print_json(lease)
        return 0
    urls = lease.get("urls") or {}
    device = lease.get("device") or {}
    rows = [("sid", lease.get("sid")), ("kind", lease.get("kind")), ("resource", lease.get("resource")),
            ("state", f"{lease.get('state')} ({lease.get('phase') or '-'})"),
            ("project", lease.get("projectId")), ("session", lease.get("sessionName")),
            ("instance", lease.get("ownerInstance")), ("owner", lease.get("ownerKind")),
            ("backend", lease.get("backendId")), ("device", device.get("name")), ("CDP", lease.get("cdp")),
            ("idle", idle_text(lease.get("idleSeconds"))), ("reason", lease.get("reason")),
            ("error", lease.get("error")), ("previous sid", lease.get("previousSid")),
            ("session URL", urls.get("session"))]
    console.print(kv_panel(f"lease {lease.get('sid')}", [(k, v) for k, v in rows if v not in (None, "")]))
    return 0


def parse_meta(text: str | None) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        fail(f"--meta must be a JSON object: {text}", EXIT_USAGE)
    if not isinstance(value, dict):
        fail("--meta must be a JSON object", EXIT_USAGE)
    return value


def capture_body(args: argparse.Namespace) -> dict:
    tags = [t.strip() for t in (getattr(args, "tags", None) or "").split(",") if t.strip()]
    body: dict = {"caption": getattr(args, "caption", None) or "", "tags": tags,
                  "meta": parse_meta(getattr(args, "meta", None)), "source": getattr(args, "source", None) or "cli"}
    pace = getattr(args, "pace", None)
    if pace:
        text = pace.strip()
        try:
            body["pace"] = json.loads(text) if text.startswith("{") else text
        except ValueError:
            body["pace"] = text
    if getattr(args, "from_here", False):
        body["fromHere"] = True
    if getattr(args, "no_pointer", False):
        body["noPointer"] = True
    hide = [h.strip() for h in (getattr(args, "hide", None) or []) if h and h.strip()]
    if hide:
        body["hide"] = hide
    return body


def print_artifact(artifact: dict, as_json: bool) -> None:
    if as_json:
        print_json(artifact)
        return
    for key in ("rawUrl", "url", "sessionUrl"):
        if artifact.get(key):
            sys.stdout.write(f"{artifact[key]}\n")
    sys.stdout.flush()


def cmd_capture_screenshot(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        artifact = client.post(f"/api/captures/{quote(args.sid)}/screenshot", json=capture_body(args), timeout=300)
    print_artifact(artifact, args.json)
    return 0


def cmd_capture_video(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        if args.action == "start":
            result = client.post(f"/api/captures/{quote(args.sid)}/video/start", json=capture_body(args), timeout=120)
            if args.json:
                print_json(result)
            else:
                ok(f"recording {result.get('resource')} for {result.get('sid')}; stop with: "
                   f"eks-harness capture video stop --sid {result.get('sid')}")
            return 0
        if args.action == "reset":
            report = client.post(f"/api/captures/{quote(args.sid)}/video/reset", json={}, timeout=900)
            if args.json:
                print_json(report)
            else:
                done = ["recorder stopped" if report.get("stoppedRecorder") else "no recorder was running"]
                if report.get("orphan"):
                    done.append("an orphaned capture was found")
                if report.get("restarted"):
                    done.append("the device was restarted; relaunch the app")
                ok(f"{report.get('device')}: {', '.join(done)}")
            return 0
        artifact = client.post(f"/api/captures/{quote(args.sid)}/video/stop", json=capture_body(args), timeout=900)
    print_artifact(artifact, args.json)
    return 0


def cmd_capture_log(args: argparse.Namespace) -> int:
    params = {"since": -abs(float(args.since))} if args.since is not None else None
    with client_from_args(args) as client:
        artifact = client.post(f"/api/captures/{quote(args.sid)}/log", json=capture_body(args), params=params,
                               timeout=600)
    print_artifact(artifact, args.json)
    return 0


def add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="json", action="store_true", help="print JSON")


def add_instance(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", help="harness instance key (default: EKS_HARNESS_INSTANCE, the Claude session, "
                                           "or tree:<work tree>)")
    parser.add_argument("--tree", help="work tree the instance belongs to (default: the current one)")


def add_capture_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sid", required=True, help="session id of the device lease")
    parser.add_argument("--caption", help="caption stored with the artifact")
    parser.add_argument("--tags", help="comma separated tags")
    parser.add_argument("--meta", help="extra metadata as a JSON object")
    parser.add_argument("--source", choices=("agent", "cli", "ui", "mcp"), help=argparse.SUPPRESS)
    parser.add_argument("--pace", help="pacing preset (demo or fast) or a JSON object of step delays")
    parser.add_argument("--from-here", dest="from_here", action="store_true",
                        help="start from the current screen instead of the post-login home")
    parser.add_argument("--no-pointer", dest="no_pointer", action="store_true",
                        help="hide the presentation cursor in videos (screenshots never carry it)")
    parser.add_argument("--hide", action="append", default=[],
                        help="extra selector to hide for one capture (repeatable)")
    add_json(parser)


def register(subparsers: argparse._SubParsersAction) -> None:
    lease = subparsers.add_parser("lease", help="acquire, resume, heartbeat, idle, release and inspect leases",
                                  description="Leases of browser profiles and devices. Every lease has a session id "
                                              "(sid) that captures and uploads use; after release the sid is "
                                              "read-only and 'lease resume <sid>' takes a new one for the same "
                                              "project and session.")
    sub = lease.add_subparsers(dest="lease_command", metavar="<action>", required=True)

    p = sub.add_parser("acquire", help="take a browser profile or a device (waits in the FIFO queue)")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--project", required=True, help="project owner/name, e.g. acme/web-app")
    p.add_argument("--session", help="session name (default: the branch of the work tree)")
    p.add_argument("--backend", help="bind the lease to a backend id; the backend lives while bound")
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT, help="seconds to wait for the lease (default 1800)")
    p.add_argument("--no-wait", action="store_true", help="return at once with the queue position")
    p.add_argument("--state-dir", help="harness state dir cleaned on release")
    p.add_argument("--label", help="label shown in the queue")
    p.add_argument("--owner-pid", type=int, help="the lease ends when this process exits (default: CLAUDE_PID)")
    p.add_argument("--shell", action="store_true", help="print shell assignments (EKS_SID, EKS_CDP_URL, ...)")
    add_instance(p)
    add_json(p)
    p.set_defaults(func=cmd_acquire)

    p = sub.add_parser("resume", help="take a new lease for the project and session of a released sid")
    p.add_argument("sid")
    p.add_argument("--backend")
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT)
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--state-dir")
    p.add_argument("--owner-pid", type=int)
    p.add_argument("--shell", action="store_true")
    add_instance(p)
    add_json(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("heartbeat", help="keep a lease (or every lease of this instance) alive")
    p.add_argument("sid", nargs="?")
    p.add_argument("--kind", choices=KINDS)
    add_instance(p)
    add_json(p)
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("idle", help="mark leases idle: they are released after the grace unless used again")
    p.add_argument("sid", nargs="?")
    p.add_argument("--grace", type=float, help="seconds (default lease.agentStopGraceSeconds)")
    add_instance(p)
    add_json(p)
    p.set_defaults(func=cmd_idle)

    p = sub.add_parser("release", help="release a lease (or this instance's leases)")
    p.add_argument("sid", nargs="?")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--reason")
    add_instance(p)
    add_json(p)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("break", help="break someone's lease by hand")
    p.add_argument("sid")
    p.add_argument("--reason")
    add_json(p)
    p.set_defaults(func=cmd_break)

    p = sub.add_parser("ended", help="the harness instance is gone: release its leases and stop its backends")
    p.add_argument("--reason")
    add_instance(p)
    add_json(p)
    p.set_defaults(func=cmd_ended)

    p = sub.add_parser("status", help="leases, queue, browsers, devices and backends")
    add_instance(p)
    add_json(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="list leases")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--project")
    p.add_argument("--session")
    p.add_argument("--instance")
    p.add_argument("--ended", action="store_true", help="include released and broken leases")
    add_json(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one lease by sid")
    p.add_argument("sid")
    add_json(p)
    p.set_defaults(func=cmd_show)

    capture = subparsers.add_parser("capture", help="device captures made by the daemon (iOS, Android)",
                                    description="Screenshots, videos and device logs of a device lease. Browser "
                                                "captures come from the web harness server.")
    capture_sub = capture.add_subparsers(dest="capture_command", metavar="<what>", required=True)
    p = capture_sub.add_parser("screenshot", help="take a screenshot")
    add_capture_options(p)
    p.set_defaults(func=cmd_capture_screenshot)
    p = capture_sub.add_parser("video", help="start or stop a video, or reset a stuck recorder")
    p.add_argument("action", choices=("start", "stop", "reset"))
    add_capture_options(p)
    p.set_defaults(func=cmd_capture_video)
    p = capture_sub.add_parser("log", help="upload a device log excerpt")
    p.add_argument("--since", type=float, help="seconds back from now (default 300)")
    add_capture_options(p)
    p.set_defaults(func=cmd_capture_log)
