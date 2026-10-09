from __future__ import annotations

import argparse
import os
import socket
import sys
from datetime import datetime
from typing import Any
from urllib.parse import quote

from eks_harness.auth import keys as auth_keys
from eks_harness.auth import users as auth_users
from eks_harness.auth import local as auth_local
from eks_harness.cli import credentials as creds
from eks_harness.cli.client import ApiClientError, HarnessClient, NotAuthenticated, client_from_args
from eks_harness.cli.ui import (
    EXIT_ERROR,
    EXIT_NO_CREDENTIALS,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    FunctionValidator,
    ask_new_password,
    ask_text,
    confirm_or_exit,
    console,
    fail,
    info,
    interactive,
    kv_panel,
    ok,
    print_json,
    require_interactive,
    table,
    warn,
)
from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths


def _when(value: Any) -> str:
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def _key_problem(text: str) -> str | None:
    return None if auth_keys.parse_api_key(text) else "expected ehk_<8 characters>_<32 characters>"


def read_secret_stdin(what: str) -> str:
    if sys.stdin is None or sys.stdin.closed:
        fail(f"{what}: nothing on standard input", EXIT_USAGE)
    value = sys.stdin.readline().rstrip("\r\n")
    if not value:
        fail(f"{what}: standard input was empty", EXIT_USAGE)
    return value


def default_key_name() -> str:
    return f"eks-harness CLI on {socket.gethostname()}"


def print_new_key(raw: str, username: str, name: str) -> None:
    console.print(kv_panel("New API key (shown once)", [("User", username), ("Name", name or ""), ("Key", raw)]))
    info("Copy it now: the daemon stores only a hash and cannot show it again.")


def register(subparsers: argparse._SubParsersAction) -> None:
    login = subparsers.add_parser("login", help="log the CLI in with an API key (pasted, never echoed)")
    login.add_argument("--key-stdin", action="store_true", help="read the API key from standard input")
    login.add_argument("--json", action="store_true", help="print JSON")
    login.set_defaults(func=cmd_login)

    logout = subparsers.add_parser("logout", help="forget the saved API key")
    logout.add_argument("--revoke", action="store_true", help="also revoke the key on the daemon")
    logout.add_argument("--json", action="store_true", help="print JSON")
    logout.set_defaults(func=cmd_logout)

    whoami = subparsers.add_parser("whoami", help="show who the CLI is logged in as")
    whoami.add_argument("--json", action="store_true", help="print JSON")
    whoami.set_defaults(func=cmd_whoami)

    keys = subparsers.add_parser("keys", help="API keys: list, create, revoke")
    keys_sub = keys.add_subparsers(dest="keys_command", metavar="<action>", required=True)
    keys_list = keys_sub.add_parser("list", help="list your keys (admins: --all or --user)")
    keys_list.add_argument("--all", action="store_true", help="every user's keys (admin)")
    keys_list.add_argument("--user", help="this user's keys (admin)")
    keys_list.add_argument("--active", action="store_true", help="hide revoked keys")
    keys_list.add_argument("--json", action="store_true", help="print JSON")
    keys_list.set_defaults(func=cmd_keys_list)
    keys_create = keys_sub.add_parser("create", help="create a key; it is printed once")
    keys_create.add_argument("--name", default="", help="a label for the key")
    keys_create.add_argument("--user", help="create it for this user (admin)")
    keys_create.add_argument("--json", action="store_true", help="print JSON")
    keys_create.set_defaults(func=cmd_keys_create)
    keys_revoke = keys_sub.add_parser("revoke", help="revoke a key by prefix or id")
    keys_revoke.add_argument("key", help="the 8-character prefix, the id, or the whole key")
    keys_revoke.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    keys_revoke.add_argument("--json", action="store_true", help="print JSON")
    keys_revoke.set_defaults(func=cmd_keys_revoke)

    users = subparsers.add_parser("users", help="users (admin): list, add, edit, enable, disable, delete")
    users_sub = users.add_subparsers(dest="users_command", metavar="<action>", required=True)
    users_list = users_sub.add_parser("list", help="list users")
    users_list.add_argument("--builtin", action="store_true", help="include the built-in local user")
    users_list.add_argument("--json", action="store_true", help="print JSON")
    users_list.set_defaults(func=cmd_users_list)
    users_add = users_sub.add_parser("add", help="add a user")
    users_add.add_argument("username")
    users_add.add_argument("--role", choices=("member", "admin"), default="member")
    password_mode = users_add.add_mutually_exclusive_group()
    password_mode.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    password_mode.add_argument("--no-password", action="store_true",
                               help="no password: the user logs in with API keys only")
    users_add.add_argument("--key", action="store_true", help="also create an API key for the user and print it once")
    users_add.add_argument("--json", action="store_true", help="print JSON")
    users_add.set_defaults(func=cmd_users_add)
    users_edit = users_sub.add_parser("edit", help="change a user")
    users_edit.add_argument("username")
    users_edit.add_argument("--rename", metavar="NEW", help="new username")
    users_edit.add_argument("--role", choices=("member", "admin"))
    edit_password = users_edit.add_mutually_exclusive_group()
    edit_password.add_argument("--password", action="store_true", help="set a new password (asked, not echoed)")
    edit_password.add_argument("--password-stdin", action="store_true", help="read the new password from standard input")
    state = users_edit.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    users_edit.add_argument("--json", action="store_true", help="print JSON")
    users_edit.set_defaults(func=cmd_users_edit)
    for action, disabled in (("disable", True), ("enable", False)):
        toggle = users_sub.add_parser(action, help=f"{action} a user")
        toggle.add_argument("username")
        toggle.add_argument("--json", action="store_true", help="print JSON")
        toggle.set_defaults(func=cmd_users_toggle, disabled=disabled)
    users_delete = users_sub.add_parser("delete", help="delete a user with their keys and grants")
    users_delete.add_argument("username")
    users_delete.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    users_delete.add_argument("--json", action="store_true", help="print JSON")
    users_delete.set_defaults(func=cmd_users_delete)

    grants = subparsers.add_parser("grants", help="grants (admin): list, add, remove")
    grants_sub = grants.add_subparsers(dest="grants_command", metavar="<action>", required=True)
    grants_list = grants_sub.add_parser("list", help="list grants")
    grants_list.add_argument("--user")
    grants_list.add_argument("--project", help="owner/name")
    grants_list.add_argument("--session", help="session name or slug (needs --project)")
    grants_list.add_argument("--json", action="store_true", help="print JSON")
    grants_list.set_defaults(func=cmd_grants_list)
    grants_add = grants_sub.add_parser("add", help="grant a user access to a project or one session")
    grants_add.add_argument("--user", required=True)
    grants_add.add_argument("--project", required=True, help="owner/name")
    grants_add.add_argument("--session", help="limit the grant to this session (name or slug)")
    grants_add.add_argument("--level", choices=("viewer", "editor"), default="viewer")
    grants_add.add_argument("--json", action="store_true", help="print JSON")
    grants_add.set_defaults(func=cmd_grants_add)
    grants_remove = grants_sub.add_parser("remove", help="remove a grant by id, or by user and project")
    grants_remove.add_argument("id", nargs="?", type=int, help="grant id from 'grants list'")
    grants_remove.add_argument("--user")
    grants_remove.add_argument("--project", help="owner/name")
    grants_remove.add_argument("--session", help="session name or slug")
    grants_remove.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    grants_remove.add_argument("--json", action="store_true", help="print JSON")
    grants_remove.set_defaults(func=cmd_grants_remove)

    auth = subparsers.add_parser("auth", help="local account recovery")
    auth_sub = auth.add_subparsers(dest="auth_command", metavar="<action>", required=True)
    recover = auth_sub.add_parser(
        "recover", help="local only: create a new admin API key (and optionally reset the password) and log in",
        description="Works on the local database directly, without the daemon and without a key: access to the "
                    "local files proves ownership. Creates the admin if it does not exist.")
    recover.add_argument("--user", default="admin", help="admin username (default: admin)")
    recover.add_argument("--reset-password", action="store_true", help="also set a new password (asked, not echoed)")
    recover.add_argument("--password-stdin", action="store_true", help="read the new password from standard input")
    recover.add_argument("--promote", action="store_true", help="make the user an admin if it is a member")
    recover.add_argument("--name", default="", help="label of the new key")
    recover.add_argument("--no-login", action="store_true", help="print the key but do not save it for the CLI")
    recover.add_argument("--json", action="store_true", help="print JSON")
    recover.set_defaults(func=cmd_auth_recover)


def cmd_login(args: argparse.Namespace) -> int:
    if args.key_stdin:
        raw = read_secret_stdin("API key").strip()
    else:
        require_interactive("The API key", EXIT_NO_CREDENTIALS)
        raw = ask_text("API key", "ehk_..., not shown", is_password=True, validator=FunctionValidator(_key_problem))
    problem = _key_problem(raw)
    if problem:
        fail(f"That is not an eks-harness API key: {problem}.", EXIT_USAGE)
    paths = resolve_paths()
    config = load_config(paths)
    with HarnessClient(base_url=args.url, api_key=raw, paths=paths, config=config) as client:
        me = client.get("/api/auth/me")
        if me.get("via") != "key" or not me.get("authEnabled"):
            fail(f"Authentication is disabled on the daemon at {client.base_url}; there is nothing to log in to. "
                 f"The key was not saved.", EXIT_ERROR)
        username = me["user"]["username"]
        path = creds.save(creds.Credentials(api_key=raw, url=client.base_url, username=username), paths)
    if creds.API_KEY_ENV in os.environ:
        warn(f"{creds.API_KEY_ENV} is set and takes precedence over the saved key.")
    if args.json:
        print_json({"username": username, "role": me["user"]["role"], "keyPrefix": me.get("keyPrefix"),
                    "url": client.base_url, "credentialsFile": str(path)})
    else:
        ok(f"Logged in as {username} ({me['user']['role']}) at {client.base_url}; key saved to {path}.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    paths = resolve_paths()
    stored = creds.load(paths)
    if args.revoke:
        if stored is None:
            fail("There is no saved key to revoke.", EXIT_NO_CREDENTIALS)
        with HarnessClient(base_url=args.url, api_key=stored.api_key, paths=paths) as client:
            client.delete(f"/api/keys/{quote(stored.prefix or '', safe='')}")
        ok(f"Revoked key {stored.prefix} on the daemon.")
    removed = creds.clear(paths)
    if removed:
        ok("Logged out: the saved key was removed.")
    else:
        info("No saved key; nothing to remove.")
    if os.environ.get(creds.API_KEY_ENV):
        warn(f"{creds.API_KEY_ENV} is still set in this environment, so commands keep using it.")
    if args.json:
        print_json({"removed": removed, "revoked": stored.prefix if args.revoke and stored else None,
                    "envKeySet": bool(os.environ.get(creds.API_KEY_ENV))})
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        try:
            me = client.get("/api/auth/me")
        except NotAuthenticated:
            if client.api_key is not None:
                raise
            fail("Not logged in: run 'eks-harness login' or set EKS_HARNESS_API_KEY.", EXIT_NO_CREDENTIALS)
        source = client.key_source
        base = client.base_url
    if args.json:
        print_json({**me, "url": base, "credentialsSource": source})
        return 0
    user = me["user"]
    rows = [("User", user["username"]), ("Role", user["role"]), ("Via", me["via"]),
            ("Auth", "enabled" if me.get("authEnabled") else "disabled (every caller is the local admin)"),
            ("Key", me.get("keyPrefix") or ""), ("Key from", {"env": creds.API_KEY_ENV, "file": "saved credentials",
                                                              "explicit": "--api-key"}.get(source, "none")),
            ("Daemon", base)]
    console.print(kv_panel("eks-harness", rows))
    if me.get("grants"):
        console.print(table(["Project", "Session", "Level"],
                            [(g["projectId"], g.get("sessionName") or "all sessions", g["level"]) for g in me["grants"]],
                            title="Grants"))
    return 0


def _keys_rows(items: list[dict]) -> list[tuple]:
    return [(k["prefix"], k.get("username") or k["userId"], k.get("name") or "", _when(k["createdAt"]),
             _when(k.get("lastUsedAt")) or "never", "active" if k.get("active") else f"revoked {_when(k.get('revokedAt'))}")
            for k in items]


def cmd_keys_list(args: argparse.Namespace) -> int:
    params = {"all": "true" if args.all else None, "user": args.user,
              "includeRevoked": "false" if args.active else None}
    with client_from_args(args) as client:
        data = client.get("/api/keys", params=params)
    if args.json:
        print_json(data)
        return 0
    if not data["items"]:
        info("No API keys. Create one with: eks-harness keys create --name <label>")
        return 0
    console.print(table(["Prefix", "User", "Name", "Created", "Last used", "State"], _keys_rows(data["items"])))
    return 0


def cmd_keys_create(args: argparse.Namespace) -> int:
    body = {"name": args.name, "username": args.user}
    with client_from_args(args) as client:
        data = client.post("/api/keys", json={k: v for k, v in body.items() if v is not None})
    if args.json:
        print_json(data)
        return 0
    print_new_key(data["key"], data.get("username") or str(data["userId"]), data.get("name") or "")
    return 0


def cmd_keys_revoke(args: argparse.Namespace) -> int:
    confirm_or_exit(f"Revoke API key {args.key}? Anything using it stops working", args.yes)
    with client_from_args(args) as client:
        data = client.delete(f"/api/keys/{quote(args.key.strip(), safe='')}")
        own = client.api_key
    if args.json:
        print_json(data)
    else:
        ok(f"Revoked key {data['prefix']} of {data.get('username') or data['userId']}.")
    if own and auth_keys.parse_api_key(own) and auth_keys.parse_api_key(own)[0] == data["prefix"]:
        warn("That was the key this CLI is using; log in again with another key.")
    return 0


def _user_rows(items: list[dict]) -> list[tuple]:
    return [(u["id"], u["username"], u["role"], "disabled" if u.get("disabled") else "enabled",
             "yes" if u.get("hasPassword") else "no", _when(u["createdAt"])) for u in items]


def cmd_users_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        data = client.get("/api/users", params={"includeBuiltin": "true" if args.builtin else None})
    if args.json:
        print_json(data)
        return 0
    if not data["items"]:
        info("No users. Add one with: eks-harness users add <username>")
        return 0
    console.print(table(["Id", "Username", "Role", "State", "Password", "Created"], _user_rows(data["items"])))
    return 0


def _password_for_add(args: argparse.Namespace) -> str | None:
    if args.no_password:
        return None
    if args.password_stdin:
        return read_secret_stdin("password")
    if not interactive():
        fail("A password is needed: pass --password-stdin, or --no-password for a key-only user.", EXIT_USAGE)
    return ask_new_password()


def cmd_users_add(args: argparse.Namespace) -> int:
    try:
        auth_users.validate_username(args.username)
    except auth_users.AccountError as problem:
        fail(problem.message, EXIT_USAGE)
    password = _password_for_add(args)
    body = {"username": args.username, "role": args.role}
    if password is not None:
        body["password"] = password
    with client_from_args(args) as client:
        user = client.post("/api/users", json=body)
        key = client.post("/api/keys", json={"username": user["username"], "name": f"{user['username']} key"}) \
            if args.key else None
    if args.json:
        print_json({"user": user, "key": key})
        return 0
    ok(f"Added {user['role']} {user['username']} (id {user['id']}).")
    if key:
        print_new_key(key["key"], user["username"], key.get("name") or "")
    if user["role"] == "member":
        info(f"Members see nothing until granted: eks-harness grants add --user {user['username']} --project <owner/name>")
    return 0


def cmd_users_edit(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.rename:
        body["username"] = args.rename
    if args.role:
        body["role"] = args.role
    if args.enable:
        body["disabled"] = False
    if args.disable:
        body["disabled"] = True
    if args.password_stdin:
        body["password"] = read_secret_stdin("password")
    elif args.password:
        require_interactive("The new password")
        body["password"] = ask_new_password("New password")
    if not body:
        fail("Nothing to change: pass --rename, --role, --password, --enable or --disable.", EXIT_USAGE)
    with client_from_args(args) as client:
        user = client.patch(f"/api/users/{quote(args.username, safe='')}", json=body)
    if args.json:
        print_json(user)
    else:
        ok(f"Updated {user['username']}: {', '.join(sorted(body))}.")
    return 0


def cmd_users_toggle(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        user = client.patch(f"/api/users/{quote(args.username, safe='')}", json={"disabled": args.disabled})
    if args.json:
        print_json(user)
    else:
        ok(f"{'Disabled' if user['disabled'] else 'Enabled'} {user['username']}.")
    return 0


def cmd_users_delete(args: argparse.Namespace) -> int:
    confirm_or_exit(f"Delete user {args.username} with all their API keys and grants", args.yes)
    with client_from_args(args) as client:
        client.delete(f"/api/users/{quote(args.username, safe='')}")
    ok(f"Deleted {args.username}.")
    if args.json:
        print_json({"deleted": args.username})
    return 0


def _grant_rows(items: list[dict]) -> list[tuple]:
    return [(g["id"], g.get("username") or g["userId"], g["projectId"],
             g.get("sessionName") or "all sessions", g["level"], _when(g["createdAt"])) for g in items]


def cmd_grants_list(args: argparse.Namespace) -> int:
    params = {"user": args.user, "project": args.project, "session": args.session}
    with client_from_args(args) as client:
        data = client.get("/api/grants", params=params)
    if args.json:
        print_json(data)
        return 0
    if not data["items"]:
        info("No grants match.")
        return 0
    console.print(table(["Id", "User", "Project", "Session", "Level", "Created"], _grant_rows(data["items"])))
    return 0


def cmd_grants_add(args: argparse.Namespace) -> int:
    body = {"username": args.user, "project": args.project, "session": args.session, "level": args.level}
    with client_from_args(args) as client:
        grant = client.post("/api/grants", json={k: v for k, v in body.items() if v is not None})
    if args.json:
        print_json(grant)
    else:
        scope = f"session {grant['sessionName']} of {grant['projectId']}" if grant.get("sessionId") else grant["projectId"]
        ok(f"{grant['username']} is now {grant['level']} on {scope} (grant {grant['id']}).")
    return 0


def cmd_grants_remove(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        if args.id is not None:
            grant_id, label = args.id, f"grant {args.id}"
        else:
            if not (args.user and args.project):
                fail("Pass a grant id, or --user and --project (and --session for a session grant).", EXIT_USAGE)
            data = client.get("/api/grants", params={"user": args.user, "project": args.project,
                                                     "session": args.session})
            matches = [g for g in data["items"] if (g.get("sessionId") is not None) == bool(args.session)]
            if not matches:
                fail(f"{args.user} has no grant on {args.project}"
                     f"{' session ' + args.session if args.session else ''}.", EXIT_NOT_FOUND)
            grant_id = matches[0]["id"]
            label = f"{args.user}'s {matches[0]['level']} grant on {args.project}" + (
                f" session {matches[0].get('sessionName')}" if args.session else "")
        confirm_or_exit(f"Remove {label}", args.yes)
        client.delete(f"/api/grants/{grant_id}")
    ok(f"Removed {label}.")
    if args.json:
        print_json({"removed": grant_id})
    return 0


def cmd_auth_recover(args: argparse.Namespace) -> int:
    password = None
    if args.password_stdin:
        password = read_secret_stdin("password")
    elif args.reset_password:
        require_interactive("The new password")
        password = ask_new_password("New password")
    paths = resolve_paths()
    try:
        with auth_local.local_database(paths) as db:
            result = auth_local.ensure_admin(db, args.user, password, promote=args.promote)
            raw, record = auth_keys.create_key(db, result.user, args.name or f"recovered on {socket.gethostname()}")
    except auth_users.AccountError as problem:
        fail(problem.message, EXIT_USAGE if problem.status < 500 else EXIT_ERROR)
    config = load_config(paths)
    saved = None
    if not args.no_login:
        with HarnessClient(base_url=args.url, api_key=raw, paths=paths, config=config) as probe:
            base = probe.base_url
        saved = creds.save(creds.Credentials(api_key=raw, url=base, username=result.user.username), paths)
    verified = _verify_quietly(args.url, raw, paths, config)
    if args.json:
        print_json({"username": result.user.username, "createdUser": result.created,
                    "passwordReset": result.password_set, "promoted": result.promoted, "key": raw,
                    "keyPrefix": record.prefix, "credentialsFile": str(saved) if saved else None,
                    "daemon": verified})
        return 0
    if result.created:
        ok(f"Created admin {result.user.username}.")
    if result.promoted:
        ok(f"Promoted {result.user.username} to admin.")
    if result.enabled:
        ok(f"Enabled {result.user.username}.")
    if result.password_set:
        ok(f"Set a new password for {result.user.username}; their web sessions were ended.")
    print_new_key(raw, result.user.username, record.name)
    if saved:
        ok(f"The CLI is logged in with it ({saved}).")
    if verified == "verified":
        ok("The running daemon accepts the key.")
    elif verified == "auth-disabled":
        warn("The daemon runs with authentication disabled; the key applies once auth is enabled.")
    elif verified == "down":
        info("The daemon is not running; the key works as soon as it starts.")
    return 0


def _verify_quietly(url: str | None, raw: str, paths, config) -> str:
    with HarnessClient(base_url=url, api_key=raw, paths=paths, config=config, timeout=5.0) as client:
        if not client.is_up():
            return "down"
        try:
            me = client.get("/api/auth/me")
        except ApiClientError as problem:
            return f"rejected: {problem.message}"
    return "verified" if me.get("via") == "key" else "auth-disabled"
