from __future__ import annotations

import argparse
import re
import sys
import time
from urllib.parse import quote

from eks_harness.cli.client import ApiClientError, HarnessClient, client_from_args
from eks_harness.cli.lease_cmds import instance_key, resolve_tree, shell_line
from eks_harness.cli.ui import EXIT_USAGE, console, err, fail, ok, print_json, table

BACKEND_FAILED = 22


def backend_path(backend_id: str, suffix: str = "") -> str:
    return f"/api/backends/{quote(backend_id, safe='@:')}" + suffix


def default_definition(tree) -> str | None:
    if tree is None:
        return None
    from eks_harness.plugins import load_project_config

    try:
        configured = load_project_config(tree).section("backends").get("default")
    except ValueError:
        configured = None
    if configured:
        return str(configured)
    folder = tree / ".harness" / "backends"
    scripts = sorted(p.stem for p in folder.glob("*.py")) if folder.is_dir() else []
    return scripts[0] if len(scripts) == 1 else None


def backend_target(args: argparse.Namespace) -> tuple[str | None, str, str]:
    tree = resolve_tree(args.tree)
    instance = instance_key(args.instance, tree)
    if not args.definition and not args.id:
        args.definition = default_definition(tree)
        if not args.definition:
            fail("pass --definition (or set [backends] default in .harness/project.toml)", EXIT_USAGE)
    return (str(tree) if tree else None), instance, args.id or f"{args.definition}@{instance}"


def read_chunk(client: HarnessClient, resource: str, offset: int | None, lines: int = 200) -> dict:
    params = {"resource": resource, "lines": lines}
    if offset is not None:
        params["offset"] = offset
    return client.get("/api/logs", params=params)


def follow_backend(client: HarnessClient, backend_id: str, wait: float, quiet: bool) -> dict:
    deadline = time.time() + wait
    offsets: dict[str, int] = {}
    while True:
        record = client.get(backend_path(backend_id))
        if not quiet:
            labels = {f"backend:{backend_id}": "prepare",
                      **{f"backend:{backend_id}:{p}": p for p in record.get("processes") or []}}
            for resource, label in labels.items():
                try:
                    chunk = read_chunk(client, resource, offsets.get(resource, -1))
                except ApiClientError:
                    continue
                if resource in offsets and chunk.get("text"):
                    for line in chunk["text"].splitlines():
                        err.print(f"[#8a93a3]{label} |[/] {line}", markup=True, highlight=False)
                offsets[resource] = chunk.get("offset", 0)
        if record.get("status") == "running":
            return record
        if record.get("status") == "failed":
            fail(f"backend {backend_id} failed:\n{record.get('error', '')}", BACKEND_FAILED)
        if time.time() > deadline:
            fail(f"backend {backend_id} not ready in {wait:.0f}s (status {record.get('status')})", BACKEND_FAILED)
        time.sleep(1)


def cmd_ensure(args: argparse.Namespace) -> int:
    tree, instance, backend_id = backend_target(args)
    with client_from_args(args) as client:
        client.post("/api/backends/ensure", json={"definition": args.definition, "instance": instance, "tree": tree,
                                                  "id": args.id, "hold": args.hold,
                                                  "restart": args.backend_command == "restart"}, timeout=300)
        record = follow_backend(client, backend_id, args.wait, args.quiet or args.shell or args.json)
    if args.shell:
        for name, value in {**(record.get("exports") or {}), "EKS_BACKEND_ID": record["id"]}.items():
            print(shell_line(name, value))
    elif args.json:
        print_json(record)
    else:
        ok(f"backend {record['id']} running: " + ", ".join(f"{k}={v}" for k, v in (record.get("ports") or {}).items()))
        for name, value in (record.get("exports") or {}).items():
            console.print(f"  {name}={value}", markup=False, highlight=False)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _, _, backend_id = backend_target(args)
    with client_from_args(args) as client:
        record = client.get(backend_path(backend_id))
    if args.json:
        print_json(record)
        return 0
    render_backend(record)
    return 0


def render_backend(record: dict) -> None:
    rows = [("id", record.get("id")), ("status", record.get("status")),
            ("definition", record.get("definition")), ("instance", record.get("instance")),
            ("ports", ", ".join(f"{k}={v}" for k, v in (record.get("ports") or {}).items()) or "-"),
            ("bound to", ", ".join(record.get("bindings") or []) or "-"),
            ("processes", ", ".join(f"{name} ({'alive' if alive else 'dead'})"
                                    for name, alive in (record.get("alive") or {}).items()) or "-")]
    if record.get("error"):
        rows.append(("error", str(record["error"]).splitlines()[0]))
    console.print(table(("field", "value"), rows, title=f"Backend {record.get('id')}"))
    for name, value in (record.get("exports") or {}).items():
        console.print(f"  {name}={value}", markup=False, highlight=False)


def cmd_stop(args: argparse.Namespace) -> int:
    _, _, backend_id = backend_target(args)
    with client_from_args(args) as client:
        record = client.post(backend_path(backend_id, "/stop"), json={"final": args.final, "reason": "requested"},
                             timeout=900)
    if args.json:
        print_json(record)
    else:
        ok(f"backend {backend_id} stopped" + (" (final teardown)" if args.final else ""))
    return 0


def cmd_hold(args: argparse.Namespace) -> int:
    _, _, backend_id = backend_target(args)
    with client_from_args(args) as client:
        record = client.post(backend_path(backend_id, "/hold"), json={"seconds": args.hold})
    if args.json:
        print_json(record)
    elif args.hold > 0:
        ok(f"backend {backend_id} held for {args.hold:g}s without a frontend")
    else:
        ok(f"backend {backend_id}: holds cleared")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        result = client.get("/api/backends")
    if args.json:
        print_json(result.get("items") or [])
        return 0
    rows = [(b.get("id"), b.get("status"), ", ".join(f"{k}={v}" for k, v in (b.get("ports") or {}).items()) or "-",
             ", ".join(b.get("bindings") or []) or "-") for b in result.get("items") or []]
    console.print(table(("id", "status", "ports", "bound to"), rows, title="Backends"))
    return 0


def cmd_definitions(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.tree)
    with client_from_args(args) as client:
        result = client.get("/api/backends/definitions", params={"tree": str(tree) if tree else None})
    if args.json:
        print_json(result.get("items") or [])
        return 0
    console.print(table(("name", "source", "path"), [(d.get("name"), d.get("source"), d.get("path"))
                                                     for d in result.get("items") or []], title="Backend definitions"))
    return 0


def cmd_backend_logs(args: argparse.Namespace) -> int:
    _, _, backend_id = backend_target(args)
    resource = f"backend:{backend_id}" + (f":{args.process}" if args.process else "")
    return show_logs(args, resource)


def show_logs(args: argparse.Namespace, resource: str) -> int:
    if args.grep:
        try:
            re.compile(args.grep)
        except re.error as error:
            fail(f"--grep is not a valid regular expression: {error}", EXIT_USAGE)
    with client_from_args(args) as client:
        if getattr(args, "json", False) and not args.follow:
            print_json(client.get("/api/logs", params={"resource": resource, "lines": args.lines, "grep": args.grep}))
            return 0
        if args.follow:
            with client.stream("GET", "/api/logs", params={"resource": resource, "lines": args.lines,
                                                           "grep": args.grep, "follow": "true"}) as response:
                for text in response.iter_text():
                    sys.stdout.write(text)
                    sys.stdout.flush()
            return 0
        chunk = client.get("/api/logs", params={"resource": resource, "lines": args.lines, "grep": args.grep})
    text = chunk.get("text", "")
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    return show_logs(args, args.resource)


def add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--definition", help="backend definition name (default: [backends] default in "
                                             ".harness/project.toml, or the only definition of the tree)")
    parser.add_argument("--id", help="backend id (default <definition>@<instance>)")
    parser.add_argument("--instance", help="harness instance key (default: this session or work tree)")
    parser.add_argument("--tree", help="work tree whose definitions are used (default: the current one)")


def add_log_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-n", "--lines", type=int, default=200)
    parser.add_argument("-f", "--follow", action="store_true")
    parser.add_argument("--grep", help="only lines matching this regular expression")


def cmd_choose(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.tree)
    with client_from_args(args) as client:
        choice = client.post("/api/backends/choose", json={"tree": str(tree) if tree else None,
                                                           "requested": args.backend})
    if args.json:
        print_json(choice)
    elif args.shell:
        print(shell_line("EKS_BACKEND", choice.get("backend") or ""))
        print(shell_line("EKS_BACKEND_TARGET", choice.get("target") or ""))
    else:
        ok(f"{choice.get('backend') or 'none'}: {choice.get('reason')}")
    return 0


def cmd_personas(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.tree)
    with client_from_args(args) as client:
        items = client.get("/api/backends/personas", params={"tree": str(tree) if tree else None,
                                                             "target": args.target})["items"]
    if args.json:
        print_json({"items": items})
        return 0
    console.print(table(["persona", "label", "user", "tenant", "roles"],
                        [(p["id"], p["label"], p.get("username") or "", p.get("tenant") or "",
                          ", ".join(p.get("roles") or [])) for p in items]))
    return 0


def cmd_seeders(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.tree)
    with client_from_args(args) as client:
        items = client.get("/api/backends/seeders", params={"tree": str(tree) if tree else None})["items"]
    if args.json:
        print_json({"items": items})
        return 0
    console.print(table(["seeder", "plugin", "backends", "scenarios"],
                        [(i["id"], i["plugin"], ", ".join(i["backends"]) or "any", ", ".join(i["scenarios"]))
                         for i in items]))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    _, _, backend_id = backend_target(args)
    with client_from_args(args) as client:
        data = client.post(backend_path(backend_id, "/seed"), json={"scenario": args.scenario, "seeder": args.seeder},
                           timeout=3600)
    if args.json:
        print_json(data)
    else:
        for item in data["items"]:
            ok(f"seeded by {item['seeder']}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    backend = subparsers.add_parser("backend", help="backends run by the daemon from definition scripts")
    sub = backend.add_subparsers(dest="backend_command", metavar="<action>", required=True)
    for name, text in (("ensure", "start the backend (or keep it) and wait until it runs"),
                       ("restart", "restart the backend and wait until it runs")):
        p = sub.add_parser(name, help=text)
        add_target(p)
        p.add_argument("--wait", type=float, default=3600)
        p.add_argument("--hold", type=float, default=300, help="keep it alive this long without a frontend")
        p.add_argument("--shell", action="store_true", help="print the backend's exports as shell assignments")
        p.add_argument("--quiet", action="store_true", help="do not stream prepare/process output while waiting")
        p.add_argument("--json", dest="json", action="store_true")
        p.set_defaults(func=cmd_ensure)
    p = sub.add_parser("status", help="the backend record")
    add_target(p)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("stop", help="stop the backend")
    add_target(p)
    p.add_argument("--final", action="store_true", help="also run the definition's final teardown")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("hold", help="keep the backend alive without a frontend (0 clears holds)")
    add_target(p)
    p.add_argument("--hold", type=float, default=300)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_hold)
    p = sub.add_parser("logs", help="the backend's prepare log or one process's output")
    add_target(p)
    p.add_argument("--process", help="one process of the backend")
    add_log_options(p)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_backend_logs)
    p = sub.add_parser("list", help="every backend the daemon knows")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("definitions", help="backend definitions found for a work tree")
    p.add_argument("--tree")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_definitions)
    p = sub.add_parser("choose", help="which backend a work tree should use (backend policy plugins)")
    p.add_argument("--tree")
    p.add_argument("--backend", help="ask for this backend; policies may still explain or override")
    p.add_argument("--shell", action="store_true", help="print EKS_BACKEND and EKS_BACKEND_TARGET")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_choose)
    p = sub.add_parser("personas", help="personas the credentials plugins offer for a target")
    p.add_argument("--tree")
    p.add_argument("--target", default="local", help="local, staging or any target a plugin knows")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_personas)
    p = sub.add_parser("seeders", help="seeders and their scenarios")
    p.add_argument("--tree")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_seeders)
    p = sub.add_parser("seed", help="seed a running backend with a scenario")
    add_target(p)
    p.add_argument("--scenario")
    p.add_argument("--seeder", help="only this seeder")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_seed)

    logs = subparsers.add_parser("logs", help="read a daemon-held log: daemon | browser:<n> | ios:<n> | android:<n> "
                                              "| backend:<id>[:<process>]")
    logs.add_argument("resource")
    add_log_options(logs)
    logs.add_argument("--json", dest="json", action="store_true")
    logs.set_defaults(func=cmd_logs)

