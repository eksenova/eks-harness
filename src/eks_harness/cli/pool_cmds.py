from __future__ import annotations

from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths
from eks_harness.pools.base import parse_device_name

import argparse
import re
from typing import Any
from urllib.parse import quote

from eks_harness.cli.client import Conflict, HarnessClient, client_from_args
from eks_harness.cli.ui import (
    EXIT_USAGE,
    confirm_or_exit,
    console,
    fail,
    kv_panel,
    ok,
    print_json,
    table,
    warn,
)

DEVICE_PATTERN = re.compile(r"^(ios|android)[:\s-]?(\d+)$", re.IGNORECASE)
PROFILE_PATTERN = re.compile(r"^(?:browser:)?(\d+):(\d+)$")
ACTION_VERBS = {"start": "Start", "shutdown": "Shut down", "reset": "Reset", "delete": "Delete"}


def parse_device(value: str) -> tuple[str, int]:
    text = value.strip()
    match = DEVICE_PATTERN.match(text)
    if match:
        return match.group(1).lower(), int(match.group(2))
    named = parse_device_name(load_config(resolve_paths()), text)
    if named:
        return named
    fail(f"not a device: {value} (use ios:1, android:2, or a pool device name)", EXIT_USAGE)


def parse_profile(value: str) -> str:
    match = PROFILE_PATTERN.match(value.strip())
    if not match:
        fail(f"not a browser profile: {value} (use browser:1:2)", EXIT_USAGE)
    return f"browser:{match.group(1)}:{match.group(2)}"


def lease_label(lease: dict | None, session: dict | None) -> str:
    if not lease:
        return "-"
    where = f"{session.get('name')}" if session else str(lease.get("ownerInstance") or "")[-24:]
    return f"{lease.get('sid')} ({where})"


def interruption_text(detail: dict) -> str:
    lease = detail.get("lease")
    if not lease:
        return ""
    session = detail.get("session") or {}
    who = session.get("name") or lease.get("ownerInstance")
    return f"; it interrupts lease {lease.get('sid')} of {who}"


def run_action(client: HarnessClient, path: str, name: str, action: str, detail: dict, confirm: str,
               args: argparse.Namespace) -> dict:
    body: dict[str, Any] = {"reason": args.reason}
    if action != "start":
        confirm_or_exit(f"{ACTION_VERBS[action]} {name}{interruption_text(detail)}?", args.yes)
        body["confirm"] = confirm
    try:
        return client.post(path, json=body, timeout=900)
    except Conflict as error:
        interrupts = (error.payload or {}).get("interrupts") or []
        if error.error == "confirmation_required" and interrupts:
            names = ", ".join(f"{i.get('sid')} ({i.get('session') or i.get('instance')})" for i in interrupts)
            warn(f"it now interrupts {names}")
        raise


def print_action(result: dict, as_json: bool) -> None:
    if as_json:
        print_json(result)
        return
    ok(result.get("message") or f"{result.get('action')} {result.get('resource')}")


def cmd_devices_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        result = client.get("/api/devices")
    if args.json:
        print_json(result)
        return 0
    rows = [(d.get("key"), d.get("name"), d.get("statusText"), lease_label(d.get("lease"), d.get("session")),
             (d.get("lease") or {}).get("ownerInstance") or "-", d.get("queueLength") or 0,
             d.get("udid") or d.get("serial") or "-") for d in result.get("items") or []]
    console.print(table(("device", "name", "status", "lease (session)", "owner", "queue", "id"), rows,
                        title=f"Devices: {result.get('running')} of {result.get('maxRunning')} running"))
    return 0


def cmd_devices_show(args: argparse.Namespace) -> int:
    kind, index = parse_device(args.device)
    with client_from_args(args) as client:
        detail = client.get(f"/api/devices/{kind}/{index}")
    if args.json:
        print_json(detail)
        return 0
    rows = [("status", detail.get("statusText")), ("udid", detail.get("udid")), ("serial", detail.get("serial")),
            ("lease", lease_label(detail.get("lease"), detail.get("session"))),
            ("session", (detail.get("session") or {}).get("url")), ("queue", detail.get("queueLength")),
            ("live", detail.get("liveUrl")), ("log", f"eks-harness logs {detail.get('logResource')}")]
    console.print(kv_panel(f"{detail.get('name')} ({detail.get('key')})", [(k, v) for k, v in rows if v not in (None, "")]))
    render_activity(detail.get("activity") or [])
    render_history(detail.get("history") or [])
    return 0


def render_activity(events: list[dict]) -> None:
    if events:
        console.print(table(("time", "event", "actor", "detail"), [
            (str(e.get("ts"))[:19], e.get("type"), e.get("actor") or "-",
             ", ".join(f"{k}={v}" for k, v in (e.get("detail") or {}).items() if v not in (None, "", [], {}))[:80])
            for e in events[:15]], title="Activity"))


def render_history(history: list[dict]) -> None:
    if history:
        console.print(table(("sid", "state", "session", "instance", "acquired", "released", "reason"), [
            (h.get("sid"), h.get("state"), h.get("sessionName") or "-", str(h.get("ownerInstance") or "")[-24:],
             str(h.get("acquiredAt") or "-")[:19], str(h.get("releasedAt") or "-")[:19], h.get("reason") or "-")
            for h in history[:15]], title="History"))


def cmd_devices_action(args: argparse.Namespace) -> int:
    kind, index = parse_device(args.device)
    with client_from_args(args) as client:
        detail = client.get(f"/api/devices/{kind}/{index}")
        result = run_action(client, f"/api/devices/{kind}/{index}/{args.action}", str(detail.get("name")),
                            args.action, detail, f"{kind}:{index}", args)
    print_action(result, args.json)
    return 0


def cmd_profiles_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        result = client.get("/api/profiles")
    if args.json:
        print_json(result)
        return 0
    rows = [(p.get("id"), p.get("statusText"), lease_label(p.get("lease"), p.get("session")),
             (p.get("lease") or {}).get("ownerInstance") or "-") for p in result.get("items") or []]
    console.print(table(("profile", "status", "lease (session)", "owner"), rows, title="Browser profiles"))
    return 0


def cmd_profiles_show(args: argparse.Namespace) -> int:
    profile = parse_profile(args.profile)
    with client_from_args(args) as client:
        detail = client.get(f"/api/profiles/{quote(profile, safe=':')}")
    if args.json:
        print_json(detail)
        return 0
    rows = [("status", detail.get("statusText")), ("lease", lease_label(detail.get("lease"), detail.get("session"))),
            ("session", (detail.get("session") or {}).get("url")), ("queue", detail.get("queueLength")),
            ("live", detail.get("liveUrl")), ("CDP", detail.get("cdp"))]
    console.print(kv_panel(profile, [(k, v) for k, v in rows if v not in (None, "")]))
    render_activity(detail.get("activity") or [])
    render_history(detail.get("history") or [])
    return 0


def cmd_profiles_action(args: argparse.Namespace) -> int:
    profile = parse_profile(args.profile)
    with client_from_args(args) as client:
        detail = client.get(f"/api/profiles/{quote(profile, safe=':')}")
        name = profile if args.action in ("start", "shutdown") else f"browser {profile.split(':')[1]} (all its profiles)"
        result = run_action(client, f"/api/profiles/{quote(profile, safe=':')}/{args.action}", name, args.action,
                            detail, profile, args)
    print_action(result, args.json)
    return 0


def cmd_browsers_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        result = client.get("/api/browsers")
    if args.json:
        print_json(result)
        return 0
    rows = [(f"browser:{b.get('index')}", b.get("statusText"), b.get("pid") or "-", b.get("port") or "-",
             b.get("binary") or "-") for b in result.get("items") or []]
    console.print(table(("browser", "status", "pid", "port", "binary"), rows,
                        title=f"Browsers ({result.get('command')}, {result.get('capacity')} profiles)"))
    return 0


def cmd_browsers_show(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        detail = client.get(f"/api/browsers/{args.index}")
    if args.json:
        print_json(detail)
        return 0
    rows = [("status", detail.get("statusText")), ("pid", detail.get("pid")), ("CDP", detail.get("cdp")),
            ("binary", detail.get("binary")), ("log", f"eks-harness logs {detail.get('logResource')}")]
    console.print(kv_panel(f"browser {args.index}", [(k, v) for k, v in rows if v not in (None, "")]))
    console.print(table(("profile", "status", "lease (session)"), [
        (p.get("id"), p.get("statusText"), lease_label(p.get("lease"), p.get("session")))
        for p in detail.get("profiles") or []], title="Profiles"))
    render_activity(detail.get("activity") or [])
    return 0


def cmd_browsers_action(args: argparse.Namespace) -> int:
    action = "shutdown" if args.action == "stop" else args.action
    with client_from_args(args) as client:
        detail = client.get(f"/api/browsers/{args.index}")
        leases = [p for p in detail.get("profiles") or [] if p.get("lease")]
        detail_for_prompt = {"lease": leases[0]["lease"], "session": leases[0].get("session")} if leases else {}
        if len(leases) > 1:
            detail_for_prompt["lease"] = {"sid": ", ".join(p["lease"].get("sid") for p in leases),
                                          "ownerInstance": f"{len(leases)} profiles"}
        result = run_action(client, f"/api/browsers/{args.index}/{action}", f"browser {args.index}", action,
                            detail_for_prompt, f"browser:{args.index}", args)
    print_action(result, args.json)
    return 0


def add_common(parser: argparse.ArgumentParser, destructive: bool) -> None:
    parser.add_argument("--reason", help="recorded with the action and on interrupted leases")
    if destructive:
        parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--json", dest="json", action="store_true", help="print JSON")


def register(subparsers: argparse._SubParsersAction) -> None:
    devices = subparsers.add_parser("devices", help="iOS simulators and Android emulators of the pool")
    sub = devices.add_subparsers(dest="devices_command", metavar="<action>", required=True)
    p = sub.add_parser("list", help="list devices with their state, lease and queue")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_devices_list)
    p = sub.add_parser("show", help="one device: lease, queue, activity and history")
    p.add_argument("device", help="ios:1, android:2 or a pool device name")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_devices_show)
    for action, text in (("start", "boot it under a manual lease owned by you"),
                         ("shutdown", "shut it down (breaks the lease holding it)"),
                         ("reset", "erase it (simctl erase, wipe the AVD data); installs nothing"),
                         ("delete", "delete the simulator or AVD; the pool recreates it on demand")):
        p = sub.add_parser(action, help=text)
        p.add_argument("device")
        add_common(p, action != "start")
        p.set_defaults(func=cmd_devices_action, action=action)

    profiles = subparsers.add_parser("profiles", help="browser profiles (isolated contexts in a pool browser)")
    sub = profiles.add_subparsers(dest="profiles_command", metavar="<action>", required=True)
    p = sub.add_parser("list", help="list profiles")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_profiles_list)
    p = sub.add_parser("show", help="one profile")
    p.add_argument("profile", help="browser:1:2")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_profiles_show)
    for action, text in (("start", "hold the profile under a manual lease and start its browser"),
                         ("shutdown", "close the profile (breaks the lease holding it)"),
                         ("reset", "erase the user data of its browser (all profiles of that browser)"),
                         ("delete", "delete the user data of its browser; recreated on demand")):
        p = sub.add_parser(action, help=text)
        p.add_argument("profile")
        add_common(p, action != "start")
        p.set_defaults(func=cmd_profiles_action, action=action)

    browsers = subparsers.add_parser("browsers", help="pool browser processes")
    sub = browsers.add_subparsers(dest="browsers_command", metavar="<action>", required=True)
    p = sub.add_parser("list", help="list browser processes")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_browsers_list)
    p = sub.add_parser("show", help="one browser process with its profiles")
    p.add_argument("index", type=int)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_browsers_show)
    for action, text in (("start", "start the browser process"),
                         ("stop", "stop it (breaks the leases of its profiles)"),
                         ("reset", "stop it and erase its user data"),
                         ("delete", "stop it and delete its user data")):
        p = sub.add_parser(action, help=text)
        p.add_argument("index", type=int)
        add_common(p, action != "start")
        p.set_defaults(func=cmd_browsers_action, action=action)
