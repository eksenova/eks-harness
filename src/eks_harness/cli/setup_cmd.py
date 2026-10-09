from __future__ import annotations

import argparse
import importlib
import inspect
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from eks_harness.auth import keys as auth_keys
from eks_harness.auth import users as auth_users
from eks_harness.auth import local as auth_local
from eks_harness.auth.exposure import exposure_problem
from eks_harness.auth.proxies import lan_addresses
from eks_harness.cli import credentials as creds
from eks_harness.cli.auth_cmds import print_new_key, read_secret_stdin
from eks_harness.cli.client import ApiClientError, HarnessClient
from eks_harness.cli.ui import (
    EXIT_NOTHING_SELECTED,
    EXIT_USAGE,
    FunctionValidator,
    Suggestion,
    SuggestionCompleter,
    ask_confirm,
    ask_new_password,
    ask_text,
    console,
    fail,
    info,
    interactive,
    kv_panel,
    ok,
    print_json,
    warn,
)
from eks_harness.config import SETTINGS, Config, ConfigError, coerce, env_name
from eks_harness.paths import Paths, resolve_paths

SERVICE_MODES = ("auto", "install", "start", "skip")
BROWSERS = (
    ("chrome", "Google Chrome"),
    ("chrome-beta", "Google Chrome Beta"),
    ("chromium", "Chromium"),
    ("edge", "Microsoft Edge"),
    ("brave", "Brave"),
)
POOL_KEYS = (
    ("browser.instances", "Browser processes"),
    ("browser.profilesPerInstance", "Profiles per browser process"),
    ("devices.ios", "iOS simulators in the pool"),
    ("devices.android", "Android emulators in the pool"),
    ("devices.maxRunning", "Devices running at the same time"),
)
ADMIN_PASSWORD_ENV = "EKS_HARNESS_SETUP_PASSWORD"
HEALTH_WAIT_SECONDS = 30.0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "setup", help="configure the daemon, create the admin and its API key, start the service",
        description="Interactive first-time setup, safe to re-run: current values are the defaults. Every prompt has "
                    "a flag; with --yes or without a terminal nothing is asked and missing values keep their "
                    "current setting. Writes the local config and database directly, so it works without a "
                    "running daemon.")
    parser.add_argument("--host", help="listen IP (127.0.0.1, 0.0.0.0 or a LAN address)")
    parser.add_argument("--port", type=int, help="listen port")
    parser.add_argument("--public-url", dest="public_url", help="base URL for links (empty string: none)")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--auth", dest="auth", action="store_true", default=None, help="enable authentication")
    auth.add_argument("--no-auth", dest="auth", action="store_false", help="disable authentication")
    parser.add_argument("--admin", help="admin username (default: the existing admin, or 'admin')")
    parser.add_argument("--admin-password-stdin", action="store_true",
                        help=f"read the admin password from standard input (or set {ADMIN_PASSWORD_ENV})")
    parser.add_argument("--keep-password", action="store_true", help="leave an existing admin password unchanged")
    parser.add_argument("--promote", action="store_true", help="promote the admin username if it is a member")
    parser.add_argument("--trusted-proxies", dest="trusted_proxies",
                        help="comma separated CIDRs of reverse proxies (empty string: none)")
    cloudflare = parser.add_mutually_exclusive_group()
    cloudflare.add_argument("--cloudflare", dest="cloudflare", action="store_true", default=None,
                            help="trust CF-Connecting-IP from trusted proxies")
    cloudflare.add_argument("--no-cloudflare", dest="cloudflare", action="store_false")
    parser.add_argument("--browser", help="browser command: chrome, chrome-beta, chromium, edge, brave or a path")
    parser.add_argument("--browser-instances", type=int, dest="browser_instances")
    parser.add_argument("--profiles-per-instance", type=int, dest="profiles_per_instance")
    parser.add_argument("--ios", type=int, help="iOS simulators in the pool")
    parser.add_argument("--android", type=int, help="Android emulators in the pool")
    parser.add_argument("--max-running", type=int, dest="max_running", help="devices running at the same time")
    parser.add_argument("--service", choices=SERVICE_MODES, default=None,
                        help="auto (default): restart a running daemon, start an installed service, else install "
                             "one; install; start; skip")
    parser.add_argument("--new-key", action="store_true", help="create a new API key even if the saved one works")
    parser.add_argument("--force", action="store_true",
                        help="allow listening on a non-loopback address with authentication disabled")
    parser.add_argument("--yes", "-y", action="store_true", help="ask nothing; use flags and current values")
    parser.add_argument("--json", action="store_true", help="print a JSON summary (includes a new key once)")
    parser.set_defaults(func=run)


def host_suggestions() -> list[Suggestion]:
    items = [Suggestion.of("127.0.0.1", "this machine only"), Suggestion.of("0.0.0.0", "every network interface")]
    items += [Suggestion.of(address, "LAN address") for address in lan_addresses()]
    return items


def _checker(key: str):
    def check(text: str) -> str | None:
        try:
            coerce(key, text)
        except ConfigError as error:
            return str(error)
        return None

    return FunctionValidator(check)


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(value) if value else "none"
    if value in (None, ""):
        return "none"
    return str(value)


@dataclass
class Plan:
    values: dict[str, Any] = field(default_factory=dict)
    admin: str | None = None
    password: str | None = None
    password_action: str = "none"


class Prompter:
    def __init__(self, args: argparse.Namespace, config: Config) -> None:
        self.args = args
        self.config = config
        self.ask = interactive() and not args.yes

    def value(self, key: str, flag: Any, label: str, *, suggestions: list[Suggestion] | None = None,
              hint: str = "") -> Any:
        current = self.config[key]
        if flag is not None:
            return coerce(key, flag)
        if not self.ask:
            return current
        setting = SETTINGS[key]
        if setting.type == "bool":
            return ask_confirm(label, default=bool(current))
        default = ",".join(current) if isinstance(current, list) else ("" if current is None else str(current))
        completer = SuggestionCompleter(suggestions) if suggestions else None
        text = ask_text(label, hint or setting.description, default=default, completer=completer,
                        validator=_checker(key), show_suggestions=completer is not None)
        return coerce(key, text)


def _existing_admin(db) -> str | None:
    candidates = [u for u in auth_local.admin_candidates(db) if not u.disabled]
    return candidates[0].username if candidates else None


def _admin_password(args: argparse.Namespace, prompter: Prompter, has_password: bool) -> tuple[str | None, str]:
    if args.admin_password_stdin:
        return read_secret_stdin("admin password"), "set"
    env_password = os.environ.get(ADMIN_PASSWORD_ENV)
    if env_password:
        return env_password, "set"
    if args.keep_password and has_password:
        return None, "keep"
    if prompter.ask:
        if has_password and not ask_confirm("Change the admin password?", default=False):
            return None, "keep"
        return ask_new_password("Admin password"), "set"
    return None, "keep" if has_password else "none"


def build_plan(args: argparse.Namespace, config: Config, paths: Paths) -> Plan:
    prompter = Prompter(args, config)
    plan = Plan()
    values = plan.values
    values["server.host"] = prompter.value("server.host", args.host, "Listen IP", suggestions=host_suggestions())
    values["server.port"] = prompter.value("server.port", args.port, "Port")
    values["server.publicUrl"] = prompter.value("server.publicUrl", args.public_url, "Public URL",
                                                hint="base of every link, e.g. https://share.example.com; empty: none")
    values["auth.enabled"] = prompter.value("auth.enabled", args.auth, "Require a login (enable authentication)?")
    problem = exposure_problem(str(values["server.host"]), bool(values["auth.enabled"]))
    if problem and not args.force:
        if not prompter.ask:
            fail(problem, EXIT_USAGE)
        warn(problem)
        if not ask_confirm(f"Listen on {values['server.host']} without authentication anyway?", default=False):
            fail("cancelled", EXIT_NOTHING_SELECTED)
        args.force = True
    if values["auth.enabled"]:
        with auth_local.local_database(paths) as db:
            default_name = _existing_admin(db) or "admin"
            if args.admin:
                name = args.admin
            elif prompter.ask:
                name = ask_text("Admin username", "letters, digits, . _ @ -", default=default_name,
                                validator=FunctionValidator(_username_problem))
            else:
                name = default_name
            try:
                auth_users.validate_username(name)
            except auth_users.AccountError as problem:
                fail(problem.message, EXIT_USAGE)
            user = db.conn().execute("SELECT password_hash FROM users WHERE username = ?", (name,)).fetchone()
        plan.admin = name
        plan.password, plan.password_action = _admin_password(args, prompter, bool(user and user[0]))
        if plan.password is not None:
            try:
                auth_users.validate_password(plan.password)
            except auth_users.AccountError as problem:
                fail(problem.message, EXIT_USAGE)
    values["server.trustedProxies"] = prompter.value(
        "server.trustedProxies", args.trusted_proxies, "Trusted proxies",
        hint="comma separated CIDRs of reverse proxies, e.g. 127.0.0.1/32; empty: none")
    values["server.cloudflare"] = prompter.value("server.cloudflare", args.cloudflare,
                                                 "Behind a Cloudflare Tunnel (trust CF-Connecting-IP)?")
    values["browser.command"] = prompter.value(
        "browser.command", args.browser, "Browser",
        suggestions=[Suggestion.of(value, meta) for value, meta in BROWSERS])
    flags = {"browser.instances": args.browser_instances, "browser.profilesPerInstance": args.profiles_per_instance,
             "devices.ios": args.ios, "devices.android": args.android, "devices.maxRunning": args.max_running}
    for key, label in POOL_KEYS:
        values[key] = prompter.value(key, flags[key], label)
    return plan


def _username_problem(text: str) -> str | None:
    try:
        auth_users.validate_username(text)
    except auth_users.AccountError as problem:
        return problem.message
    return None


def _service_module():
    try:
        return importlib.import_module("eks_harness.service")
    except ModuleNotFoundError as error:
        if error.name != "eks_harness.service":
            raise
        return None


def _call(function, paths: Paths, config: Config) -> Any:
    parameters = inspect.signature(function).parameters
    available = {"paths": paths, "config": config}
    return function(**{name: value for name, value in available.items() if name in parameters})


def _wait_for_health(client: HarnessClient, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if client.is_up(timeout=1.0):
            return True
        time.sleep(0.5)
    return False


def manage_service(mode: str, paths: Paths, config: Config, api_key: str | None, was_up_url: str | None,
                   restart_needed: bool) -> str:
    if mode == "skip":
        return "skipped"
    if was_up_url and mode in ("auto", "start"):
        if not restart_needed:
            return "running"
        with HarnessClient(base_url=was_up_url, api_key=api_key, paths=paths, config=config) as client:
            try:
                client.post("/api/daemon/restart")
            except ApiClientError as problem:
                warn(f"Could not ask the running daemon to restart ({problem.message}); run 'eks-harness daemon "
                     f"restart' to apply the new settings.")
                return "restart-failed"
        return "restarting"
    service = _service_module()
    if service is None:
        warn("This installation has no service module (eks_harness.service); start the daemon with "
             "'eks-harness daemon start'.")
        return "unavailable"
    try:
        installed = bool(_call(service.installed, paths, config)) if hasattr(service, "installed") else False
        if mode == "install" or (mode == "auto" and not installed):
            _call(service.install, paths, config)
            return "installed"
        if not installed:
            warn("The daemon is not installed as a service; install it with 'eks-harness daemon install' or start "
                 "it with 'eks-harness daemon start'.")
            return "not-installed"
        _call(service.start, paths, config)
    except (OSError, RuntimeError) as problem:
        warn(f"The service step failed: {problem}")
        return "failed"
    return "started"


def run(args: argparse.Namespace) -> int:
    paths = resolve_paths().ensure()
    config = Config(paths)
    old_values = config.as_dict()
    was_up_url = None
    with HarnessClient(base_url=args.url, paths=paths, config=config, timeout=3.0) as probe:
        if probe.is_up(timeout=1.5):
            was_up_url = probe.base_url
    try:
        plan = build_plan(args, config, paths)
    except ConfigError as problem:
        fail(str(problem), EXIT_USAGE)
    values = plan.values
    problem = exposure_problem(str(values["server.host"]), bool(values["auth.enabled"]))
    if problem and not args.force:
        fail(problem, EXIT_USAGE)
    if problem:
        warn("Listening on a network address without authentication, as forced.")
    changes = {key: value for key, value in values.items() if old_values.get(key) != value}
    overridden = [key for key in changes if config.source(key) == "env"]
    mode = args.service or "auto"

    if not args.json:
        rows = [(key, _display(value)) for key, value in values.items()]
        if plan.admin:
            rows.append(("admin", plan.admin))
            rows.append(("admin password", {"set": "set now", "keep": "unchanged", "none": "none (key login only)"}[
                plan.password_action]))
        rows.append(("service", mode))
        console.print(kv_panel("eks-harness setup", rows))
        for key in overridden:
            warn(f"{key} is also set by the environment ({env_name(key)}); the environment value wins.")
    if interactive() and not args.yes and not ask_confirm("Apply these settings?", default=True):
        fail("cancelled", EXIT_NOTHING_SELECTED)

    if changes:
        config.set_many(changes)
    raw_key = None
    key_record = None
    kept_key = None
    admin_result = None
    if values["auth.enabled"]:
        with auth_local.local_database(paths) as db:
            try:
                admin_result = auth_local.ensure_admin(db, plan.admin, plan.password, promote=args.promote)
            except auth_users.AccountError as problem_:
                fail(problem_.message, EXIT_USAGE)
            stored = creds.load(paths)
            kept = auth_local.key_belongs_to(db, stored.api_key if stored else None, admin_result.user.id)
            if kept is not None and not args.new_key:
                kept_key = stored.api_key
            else:
                raw_key, key_record = auth_keys.create_key(db, admin_result.user,
                                                          f"eks-harness CLI on {socket.gethostname()}")
    api_key = raw_key or kept_key
    local_url = config.local_url()
    if api_key:
        creds.save(creds.Credentials(api_key=api_key, url=args.url or local_url,
                                     username=admin_result.user.username if admin_result else None), paths)

    restart_needed = bool(changes)
    service_state = manage_service(mode, paths, config, api_key, was_up_url, restart_needed)
    daemon_state = "unknown"
    if service_state not in ("skipped", "unavailable", "failed", "not-installed", "restart-failed"):
        if service_state == "restarting":
            time.sleep(1.0)
        with HarnessClient(base_url=args.url, api_key=api_key, paths=paths, config=config, timeout=5.0) as client:
            if _wait_for_health(client, HEALTH_WAIT_SECONDS):
                daemon_state = "up"
                if api_key and values["auth.enabled"]:
                    try:
                        me = client.get("/api/auth/me")
                        daemon_state = "verified" if me.get("via") == "key" else "up (auth not active yet)"
                    except ApiClientError as problem_:
                        daemon_state = f"key rejected: {problem_.message}"
            else:
                daemon_state = "not reachable"

    if args.json:
        print_json({
            "configFile": str(config.file), "changed": sorted(changes), "settings": values,
            "admin": plan.admin, "adminCreated": bool(admin_result and admin_result.created),
            "passwordSet": plan.password_action == "set",
            "key": raw_key, "keyPrefix": key_record.prefix if key_record else None, "keptExistingKey": bool(kept_key),
            "credentialsFile": str(paths.credentials_file) if api_key else None,
            "service": service_state, "daemon": daemon_state, "url": local_url, "publicUrl": config.public_url(),
        })
        return 0

    ok(f"Settings saved to {config.file}" + (f" ({len(changes)} changed)." if changes else " (nothing changed)."))
    if admin_result:
        if admin_result.created:
            ok(f"Created admin {admin_result.user.username}.")
        elif admin_result.promoted:
            ok(f"Promoted {admin_result.user.username} to admin.")
        if plan.password_action == "set" and not admin_result.created:
            ok(f"Updated the password of {admin_result.user.username}.")
        if plan.password_action == "none":
            warn("The admin has no password; log in to the web UI with the API key.")
    if raw_key:
        print_new_key(raw_key, admin_result.user.username, key_record.name)
        ok(f"The CLI is logged in with it ({paths.credentials_file}).")
    elif kept_key:
        ok("Kept the saved API key; it still belongs to this admin (use --new-key for a fresh one).")
    elif not values["auth.enabled"]:
        info("Authentication is off: every local caller acts as the built-in admin, no key is needed.")
    service_text = {"skipped": "Service step skipped.", "running": "The daemon is running with these settings.",
                    "restarting": "Asked the running daemon to restart with the new settings.",
                    "restart-failed": "The daemon still runs with its old settings.",
                    "installed": "Installed the login service.", "started": "Started the service.",
                    "not-installed": "No service was started.",
                    "unavailable": "No service was started.",
                    "failed": "The service could not be installed or started."}[service_state]
    if service_state in ("restart-failed", "not-installed", "unavailable", "failed"):
        warn(service_text)
    else:
        ok(service_text)
    if daemon_state in ("up", "verified"):
        ok(f"Daemon at {local_url}: {daemon_state}.")
    elif daemon_state != "unknown":
        warn(f"Daemon at {local_url}: {daemon_state}.")
    console.print(kv_panel("Open", [("Web UI", config.public_url()), ("Local", local_url)]))
    return 0

