from __future__ import annotations

import argparse
import time

from eks_harness import service
from eks_harness.cli.backend_cmds import show_logs
from eks_harness.cli.client import NotAuthenticated, client_from_args
from eks_harness.cli.lease_cmds import render_status
from eks_harness.cli.ui import (
    EXIT_DAEMON_DOWN,
    EXIT_ERROR,
    EXIT_OK,
    confirm_or_exit,
    console,
    fail,
    info,
    ok,
    print_json,
    warn,
)
from eks_harness.config import load as load_config
from eks_harness.daemon import server
from eks_harness.paths import resolve_paths

START_TIMEOUT = 45.0
STOP_TIMEOUT = 60.0


def cmd_serve(args: argparse.Namespace) -> int:
    options = server.ServeOptions(managed=args.managed, host=args.host, port=args.port, force=args.force,
                                  fake_pools=True if args.fake_pools else None,
                                  argv=server.serve_argv(args.managed, args.host, args.port, args.force,
                                                         args.fake_pools))
    try:
        return server.serve(options)
    except server.ServeError as error:
        if error.code == 0:
            info(str(error))
        else:
            fail(str(error), error.code)
        return error.code


def start_daemon(paths, wait: float) -> tuple[dict, str]:
    running = server.running_daemon(paths)
    if running and server.health_ok(running):
        return running, "already running"
    if service.installed(paths):
        service.start(paths)
        how = "started by the service"
    else:
        server.spawn_daemon(paths)
        how = "started on demand"
    found = server.wait_until_up(paths, wait)
    if not found:
        fail(f"the daemon did not start within {wait:.0f}s; log: {paths.daemon_log}", EXIT_ERROR)
    return found, how


def cmd_start(args: argparse.Namespace) -> int:
    paths = resolve_paths().ensure()
    found, how = start_daemon(paths, args.wait)
    if args.json:
        print_json({"running": True, "url": found.get("url"), "pid": found.get("pid"),
                    "managed": found.get("managed"), "how": how})
    else:
        ok(f"daemon {how}: {found.get('url')} (pid {found.get('pid')})")
    return EXIT_OK


def stop_daemon(paths, timeout: float) -> bool:
    running = server.running_daemon(paths)
    if not running:
        return False
    server.control_request(running, "stop")
    if not server.wait_until_down(paths, int(running["pid"]), running.get("started") or None, timeout):
        fail(f"the daemon (pid {running['pid']}) did not stop within {timeout:.0f}s", EXIT_ERROR)
    return True


def cmd_stop(args: argparse.Namespace) -> int:
    paths = resolve_paths()
    try:
        stopped = stop_daemon(paths, args.timeout)
    except server.ServeError as error:
        fail(str(error), EXIT_ERROR)
    if args.json:
        print_json({"stopped": stopped, "running": False, "service": service.installed(paths)})
        return EXIT_OK
    if not stopped:
        info("the daemon is not running")
        return EXIT_OK
    suffix = " (the service starts it again at the next login or with 'eks-harness daemon start')" \
        if service.installed(paths) else ""
    ok("daemon stopped" + suffix)
    return EXIT_OK


def cmd_restart(args: argparse.Namespace) -> int:
    paths = resolve_paths().ensure()
    running = server.running_daemon(paths)
    if not running:
        found, how = start_daemon(paths, args.wait)
        report_restart(args, found, f"the daemon was not running; {how}", "started")
        return EXIT_OK
    if args.hard:
        stop_daemon(paths, STOP_TIMEOUT)
        found, how = start_daemon(paths, args.wait)
        report_restart(args, found, f"daemon restarted ({how})", "hard")
        return EXIT_OK
    before = running.get("startedAt")
    try:
        server.control_request(running, "restart")
    except server.ServeError as error:
        fail(str(error), EXIT_ERROR)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        current = server.running_daemon(paths)
        if current and current.get("startedAt") != before and server.health_ok(current):
            report_restart(args, current, "daemon restarted gracefully (leases, devices, browsers and backends kept)",
                           "graceful")
            return EXIT_OK
        time.sleep(0.25)
    fail(f"the daemon did not come back within {args.wait:.0f}s; log: {paths.daemon_log}", EXIT_ERROR)
    return EXIT_ERROR


def report_restart(args: argparse.Namespace, found: dict, message: str, how: str) -> None:
    if args.json:
        print_json({"restarted": True, "how": how, "url": found.get("url"), "pid": found.get("pid")})
    else:
        ok(f"{message}: {found.get('url')}")


def cmd_status(args: argparse.Namespace) -> int:
    paths = resolve_paths()
    config = load_config(paths)
    running = server.running_daemon(paths)
    with client_from_args(args) as client:
        if not client.is_up():
            data = {"running": False, "url": client.base_url, "localUrl": config.local_url(),
                    "service": service.installed(paths), "logFile": str(paths.daemon_log)}
            if args.json:
                print_json(data)
            else:
                info("the daemon is not running" + (" (installed as a service)" if data["service"] else ""))
            return EXIT_DAEMON_DOWN
        try:
            status = client.get("/api/status")
        except NotAuthenticated as error:
            data = {"running": True, "url": client.base_url, "pid": (running or {}).get("pid"),
                    "authRequired": True, "message": error.message}
            if args.json:
                print_json(data)
            else:
                warn(f"the daemon runs at {client.base_url}, but {error.message}")
            return error.exit_code
    status["running"] = True
    status["localUrl"] = config.local_url()
    status["service"] = service.installed(paths)
    status["logFile"] = str(paths.daemon_log)
    if args.json:
        print_json(status)
        return EXIT_OK
    render_status(status, None)
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    paths = resolve_paths().ensure()
    running = server.running_daemon(paths)
    if running:
        server.control_request(running, "stop")
        server.wait_until_down(paths, int(running["pid"]), running.get("started") or None, STOP_TIMEOUT)
    installed = service.install(paths, retire=list(load_config(paths)["service.retireLabels"] or []))
    found = server.wait_until_up(paths, args.wait)
    for removed in installed.get("retired") or []:
        info(f"removed the replaced service: {removed}")
    hint = service.linger_hint()
    if hint:
        info(hint)
    if args.json:
        print_json({**installed, "running": bool(found), "url": (found or {}).get("url")})
    else:
        ok(f"installed: {installed['where']} -> {' '.join(installed['command'])}; "
           + (f"running at {found.get('url')}" if found else f"not answering yet, see {installed['log']}"))
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    paths = resolve_paths()
    confirm_or_exit("Remove the eks-harness login service? The daemon stops now", args.yes)
    removed = service.uninstall(paths)
    running = server.running_daemon(paths)
    if running:
        try:
            stop_daemon(paths, STOP_TIMEOUT)
        except server.ServeError as error:
            warn(str(error))
    if args.json:
        print_json({"uninstalled": bool(removed), "where": (removed or {}).get("where"), "running": False})
        return EXIT_OK
    ok(f"uninstalled ({removed['where']})" if removed else "no service was installed")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    return show_logs(args, "daemon")


def cmd_sweep(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        report = client.post("/api/daemon/sweep", json={}, timeout=900)
    if args.json:
        print_json(report)
        return EXIT_OK
    render_report("sweep", report)
    return EXIT_OK


def cmd_housekeeping(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        report = client.post("/api/daemon/housekeeping", json={}, timeout=900)
    if args.json:
        print_json(report)
        return EXIT_OK
    render_report("housekeeping", report)
    return EXIT_OK


def render_report(what: str, report: dict) -> None:
    if report.get("skipped"):
        info(f"{what} skipped: {report['skipped']}")
        return
    lines = list(report_lines(report))
    if not lines:
        ok(f"{what} done; nothing to report")
        return
    ok(f"{what} done")
    for line in lines:
        console.print(f"  {line}", markup=False, highlight=False)


def report_lines(value: object, label: str = ""):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from report_lines(inner, f"{label}.{key}" if label else str(key))
        return
    if isinstance(value, list):
        if value:
            yield f"{label}: {', '.join(str(item) for item in value)}"
        return
    if value not in (None, 0, "", False):
        yield f"{label}: {value}"


def cmd_url(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        url = client.base_url
    if args.json:
        print_json({"url": url})
    else:
        print(url)
    return EXIT_OK


def register(subparsers: argparse._SubParsersAction) -> None:
    daemon = subparsers.add_parser(
        "daemon", help="run, start, stop, restart and install the daemon",
        description="Process management works on local files (it needs no API key): serve, start, stop, restart, "
                    "install, uninstall. status, logs, sweep and housekeeping go through the API.")
    sub = daemon.add_subparsers(dest="daemon_command", metavar="<action>", required=True)

    p = sub.add_parser("serve", help="run the daemon in the foreground (what the service runs)")
    p.add_argument("--managed", action="store_true", help="run as the login service: never idle-exit, take over")
    p.add_argument("--host", help="listen address for this run (default server.host)")
    p.add_argument("--port", type=int, help="port for this run (default server.port)")
    p.add_argument("--force", action="store_true", help="listen on a non-loopback address with auth disabled")
    p.add_argument("--fake-pools", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("start", help="start the daemon (through the service when installed)")
    p.add_argument("--wait", type=float, default=START_TIMEOUT)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop the daemon; browsers, devices and backends keep running")
    p.add_argument("--timeout", type=float, default=STOP_TIMEOUT)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("restart", help="restart gracefully: persisted state is reloaded and nothing is broken")
    p.add_argument("--hard", action="store_true", help="stop and start instead of re-executing in place")
    p.add_argument("--wait", type=float, default=START_TIMEOUT)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("status", help="daemon, leases, queue, pools and backends")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("install", help="run the daemon as a login service around the clock")
    p.add_argument("--wait", type=float, default=START_TIMEOUT)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove the login service and stop the daemon")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("logs", help="the daemon's own log")
    p.add_argument("-n", "--lines", type=int, default=200)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--grep")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("sweep", help="stop stray browsers and emulators, delete legacy simulators")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("housekeeping", help="run one housekeeping pass now (ticks, retention, pruning)")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_housekeeping)

    p = sub.add_parser("url", help="print the daemon URL the CLI uses")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_url)
