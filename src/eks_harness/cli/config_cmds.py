from __future__ import annotations

import argparse
import json
from typing import Any

from eks_harness.auth import local as auth_local
from eks_harness.auth.exposure import exposure_problem
from eks_harness.cli.client import HarnessClient, client_from_args
from eks_harness.cli.ui import EXIT_CONFLICT, EXIT_OK, EXIT_USAGE, console, fail, info, ok, print_json, table, warn
from eks_harness.config import (
    DEFAULTS,
    RESTART_REQUIRED,
    SETTINGS,
    Config,
    ConfigError,
    describe,
    env_name,
    parse_cli_value,
)
from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths

RESTART_HINT = "run 'eks-harness daemon restart' to apply it"


def render(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, (list, dict, bool)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def require_key(key: str) -> str:
    if key not in SETTINGS:
        close = [k for k in SETTINGS if key.lower() in k.lower()]
        hint = f" Did you mean: {', '.join(close[:5])}?" if close else " Run 'eks-harness config show' for the list."
        fail(f"Unknown setting '{key}'.{hint}", EXIT_USAGE)
    return key


def daemon_client(args: argparse.Namespace) -> HarnessClient | None:
    if getattr(args, "local", False):
        return None
    client = client_from_args(args)
    if client.is_up():
        return client
    client.close()
    return None


def local_rows(config: Config) -> dict[str, Any]:
    return {"settings": describe(config), "configFile": str(config.file), "restartPending": False,
            "restartPendingKeys": [], "errors": config.errors(), "via": "file"}


def load_rows(args: argparse.Namespace) -> dict[str, Any]:
    client = daemon_client(args)
    if client is None:
        return local_rows(load_config(resolve_paths()))
    with client:
        data = client.get("/api/settings")
    data["via"] = "daemon"
    return data


def cmd_show(args: argparse.Namespace) -> int:
    data = load_rows(args)
    rows = data.get("settings") or []
    if args.group:
        rows = [r for r in rows if r.get("group") == args.group]
        if not rows:
            groups = sorted({r.get("group") for r in data.get("settings") or []})
            fail(f"No settings in group '{args.group}'. Groups: {', '.join(groups)}", EXIT_USAGE)
    if args.json:
        print_json({**data, "settings": rows})
        return EXIT_OK
    source = "the running daemon" if data["via"] == "daemon" else "the config file (the daemon is not running)"
    console.print(table(
        ["Key", "Value", "Default", "Source", "Restart"],
        [(r["key"], render(r.get("value")), render(r.get("default")), r.get("source"),
          ("pending" if r.get("pendingRestart") else "needed") if r.get("restartRequired") else "")
         for r in rows],
        title=f"Settings from {source}"))
    info(f"Config file: {data.get('configFile')}")
    for key, problem in (data.get("errors") or {}).items():
        warn(f"{key}: {problem}")
    if data.get("restartPending"):
        warn(f"Changed settings wait for a restart ({', '.join(data.get('restartPendingKeys') or [])}); "
             f"{RESTART_HINT}.")
    return EXIT_OK


def cmd_get(args: argparse.Namespace) -> int:
    key = require_key(args.key)
    data = load_rows(args)
    row = next((r for r in data.get("settings") or [] if r.get("key") == key), None)
    if row is None:
        fail(f"The daemon did not report the setting {key}.")
    if args.json:
        print_json({**row, "via": data["via"]})
    else:
        print(render(row.get("value")))
    return EXIT_OK


def report_changes(args: argparse.Namespace, changes: list[dict], restart_required: bool, via: str) -> int:
    if args.json:
        print_json({"changed": changes, "restartRequired": restart_required, "via": via})
        return EXIT_OK
    if not changes:
        info("Nothing changed.")
        return EXIT_OK
    for change in changes:
        ok(f"{change['key']}: {render(change.get('old'))} -> {render(change.get('new'))}")
    if restart_required and via == "daemon":
        warn(f"The daemon needs a restart for this change; {RESTART_HINT}.")
    elif via == "file":
        info("The daemon is not running; the change applies when it starts.")
    return EXIT_OK


def patch_daemon(client: HarnessClient, args: argparse.Namespace, body: dict) -> int:
    with client:
        result = client.patch("/api/settings", json=body, params={"force": "true"} if args.force else None)
    changes = [{"key": c["key"], "old": c.get("old"), "new": c.get("new"),
                "restartRequired": c.get("restartRequired", False)} for c in result.get("changed") or []]
    return report_changes(args, changes, bool(result.get("restartRequired")), "daemon")


def check_local(config: Config, values: dict[str, Any], unset: list[str], force: bool) -> None:
    for key in [*values, *unset]:
        if config.source(key) == "env":
            fail(f"{key} is set by the environment variable {env_name(key)}; change it there.", EXIT_USAGE)

    def prospective(key: str) -> Any:
        if key in values:
            return values[key]
        if key in unset:
            return DEFAULTS[key]
        return config[key]

    if prospective("backend.portRangeStart") > prospective("backend.portRangeEnd"):
        fail("backend.portRangeStart must not be above backend.portRangeEnd.", EXIT_USAGE)
    problem = exposure_problem(str(prospective("server.host")), bool(prospective("auth.enabled")))
    if problem and not force:
        fail(problem, EXIT_CONFLICT)
    if bool(prospective("auth.enabled")) and not config["auth.enabled"] and not force:
        with auth_local.local_database(config.paths) as db:
            usable = auth_local.usable_admin_exists(db)
        if not usable:
            fail("Enabling auth now would lock everyone out: no enabled admin has a password or an API key. Run "
                 "'eks-harness setup' or 'eks-harness auth recover' first, or pass --force.", EXIT_CONFLICT)


def write_local(args: argparse.Namespace, values: dict[str, Any], unset: list[str]) -> int:
    config = load_config(resolve_paths())
    check_local(config, values, unset, args.force)
    before = config.as_dict()
    if values:
        config.set_many(values)
    for key in unset:
        config.unset(key)
    after = config.as_dict()
    changes = [{"key": key, "old": before.get(key), "new": after.get(key), "restartRequired": key in RESTART_REQUIRED}
               for key in [*values, *unset] if before.get(key) != after.get(key)]
    return report_changes(args, changes, any(c["restartRequired"] for c in changes), "file")


def parse_pairs(pairs: list[str]) -> dict[str, Any]:
    if len(pairs) % 2:
        fail("Pass settings as KEY VALUE pairs, for example: config set server.port 7272", EXIT_USAGE)
    values: dict[str, Any] = {}
    for key, raw in zip(pairs[0::2], pairs[1::2]):
        require_key(key)
        try:
            values[key] = parse_cli_value(key, raw)
        except ConfigError as problem:
            fail(str(problem), EXIT_USAGE)
    return values


def cmd_set(args: argparse.Namespace) -> int:
    values = parse_pairs(args.pairs)
    client = daemon_client(args)
    if client is not None:
        return patch_daemon(client, args, {"values": values})
    return write_local(args, values, [])


def cmd_unset(args: argparse.Namespace) -> int:
    keys = [require_key(key) for key in args.keys]
    client = daemon_client(args)
    if client is not None:
        return patch_daemon(client, args, {"unset": keys})
    return write_local(args, {}, keys)


def cmd_path(args: argparse.Namespace) -> int:
    paths = resolve_paths()
    data = {"configFile": str(paths.config_file), "configDir": str(paths.config_dir), "dataDir": str(paths.data_dir),
            "cacheDir": str(paths.cache_dir), "stateDir": str(paths.state_dir), "logDir": str(paths.log_dir)}
    if args.json:
        print_json(data)
    else:
        for key, value in data.items():
            print(f"{key}: {value}")
    return EXIT_OK


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "config", help="show and change daemon settings",
        description="Reads and changes settings through the daemon's API when it is running (admin only; the change "
                    "is audited and validated), and the local config file when it is not.")
    sub = parser.add_subparsers(dest="config_command", metavar="<action>", required=True)

    p = sub.add_parser("show", help="every setting with its value, default and source")
    p.add_argument("--group", help="only one group, for example server or devices")
    p.add_argument("--local", action="store_true", help="read the config file even when the daemon runs")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("get", help="the value of one setting")
    p.add_argument("key")
    p.add_argument("--local", action="store_true", help="read the config file even when the daemon runs")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help="change one or more settings: set KEY VALUE [KEY VALUE ...]")
    p.add_argument("pairs", nargs="+", metavar="KEY VALUE")
    p.add_argument("--force", action="store_true",
                   help="accept a non-loopback bind with auth off, or enabling auth without a usable admin")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("unset", help="return settings to their defaults")
    p.add_argument("keys", nargs="+", metavar="KEY")
    p.add_argument("--force", action="store_true", help="accept an unsafe result (see set --force)")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_unset)

    p = sub.add_parser("path", help="where the config, data, cache, state and logs live")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_path)
