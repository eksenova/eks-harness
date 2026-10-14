from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from eks_harness.cli.client import ApiClientError, HarnessClient, client_from_args
from eks_harness.cli.lease_cmds import branch_of, instance_key, resolve_tree, shell_line
from eks_harness.cli.ui import (
    EXIT_USAGE,
    console,
    err,
    fail,
    info,
    ok,
    print_json,
    table,
    write_line,
)
from eks_harness.durations import describe_duration
from eks_harness.pools.backend_info import backend_identity

BACKEND_FAILED = 22
POLL_SLICE = 25.0


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


def parse_params(values: list[str] | None) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            fail(f"--param takes KEY=VALUE (got {item!r})", EXIT_USAGE)
        key, _, value = item.partition("=")
        params[key.strip()] = value
    return params


def backend_target(args: argparse.Namespace) -> tuple[str | None, str, str]:
    lease = getattr(args, "lease", None)
    if lease and not args.id:
        with client_from_args(args) as client:
            found = client.get(f"/api/backend-leases/{quote(lease)}")
        backend = (found.get("lease") or {}).get("backendId") or ""
        return None, backend.split("@", 1)[-1], backend
    tree = resolve_tree(args.tree)
    if not args.definition and not args.id:
        args.definition = default_definition(tree)
        if not args.definition:
            fail("pass --definition (or set [backends] default in .harness/project.toml)", EXIT_USAGE)
    if args.instance:
        identity = args.instance
    else:
        identity = backend_identity(str(tree) if tree else None, getattr(args, "key", None),
                                    parse_params(getattr(args, "param", None)))
    return (str(tree) if tree else None), identity, args.id or f"{args.definition}@{identity}"


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
    holder = optional_instance(None, resolve_tree(None)) or "cli"
    with client_from_args(args) as client:
        client.post("/api/backends/ensure", json={"definition": args.definition, "instance": instance, "tree": tree,
                                                  "id": args.id, "hold": args.hold, "holdId": holder,
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
            write_line(f"  {name}={value}")
    return 0


def optional_instance(explicit: str | None, tree: Path | None) -> str | None:
    if explicit or tree is not None or os.environ.get("EKS_HARNESS_INSTANCE") or os.environ.get(
            "CLAUDE_CODE_SESSION_ID"):
        return instance_key(explicit, tree)
    return None


def holder_name(args: argparse.Namespace, tree: Path | None) -> str:
    return args.holder or optional_instance(args.instance_owner, tree) or "cli"


def info_exports(found: dict, lease: dict) -> dict[str, Any]:
    exports: dict[str, Any] = {"EKS_BACKEND_LEASE": lease.get("id"), "EKS_BACKEND_ID": lease.get("backendId"),
                               "EKS_BACKEND_URL": found.get("url") or ""}
    for name, url in (found.get("urls") or {}).items():
        exports[f"EKS_BACKEND_URL_{re.sub(r'[^A-Za-z0-9]+', '_', name).upper()}"] = url
    for name, value in (found.get("env") or {}).items():
        exports[name] = value
    if found.get("credentials") and not found.get("redacted"):
        exports["EKS_BACKEND_CREDENTIALS"] = json.dumps(found["credentials"], separators=(",", ":"))
    return exports


def wait_backend_lease(client: HarnessClient, response: dict, wait: float, quiet: bool, secrets: bool) -> dict:
    lease = response.get("lease") or {}
    deadline = time.time() + wait
    offsets: dict[str, int] = {}
    while response.get("status") == "starting" and time.time() < deadline:
        backend = response.get("backend") or {}
        if not quiet:
            backend_id = backend.get("id") or lease.get("backendId")
            labels = {f"backend:{backend_id}": "prepare",
                      **{f"backend:{backend_id}:{p}": p for p in backend.get("processes") or []}}
            for resource, label in labels.items():
                try:
                    chunk = read_chunk(client, resource, offsets.get(resource, -1))
                except ApiClientError:
                    continue
                if resource in offsets and chunk.get("text"):
                    for line in chunk["text"].splitlines():
                        err.print(f"[#8a93a3]{label} |[/] {line}", markup=True, highlight=False, soft_wrap=True)
                offsets[resource] = chunk.get("offset", 0)
        slice_seconds = min(5.0 if not quiet else POLL_SLICE, max(0.0, deadline - time.time()))
        response = client.get(f"/api/backend-leases/{quote(lease['id'])}",
                              params={"waitSeconds": round(slice_seconds, 1), "secrets": str(secrets).lower()},
                              timeout=slice_seconds + 30)
    return response


def print_backend_lease(response: dict, args: argparse.Namespace) -> int:
    lease = response.get("lease") or {}
    backend = response.get("backend") or {}
    found = response.get("info") or {}
    if response.get("status") == "failed":
        if args.json:
            print_json(response)
        fail(f"backend {backend.get('id')} failed (lease {lease.get('id')} is still held; release it with "
             f"eks-harness backend release {lease.get('id')}):\n{response.get('error') or ''}", BACKEND_FAILED)
    if args.json:
        print_json(response)
        return 0
    if args.shell:
        for name, value in info_exports(found, lease).items():
            print(shell_line(name, value))
        return 0
    state = "ready" if response.get("status") == "granted" else str(response.get("status"))
    ok(f"backend {backend.get('id')} {state}; lease {lease.get('id')} held by {lease.get('holder')} "
       f"(renew within {describe_duration(lease.get('ttl')) if lease.get('ttl') else 'never'}, "
       f"{len(backend.get('leases') or [])} lease(s) in total)")
    write_line(str(lease.get("id")))
    if found.get("url"):
        write_line(found["url"])
    render_info(found, urls_only=True)
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.tree)
    if not args.definition:
        args.definition = default_definition(tree)
        if not args.definition:
            fail("pass --definition (or set [backends] default in .harness/project.toml)", EXIT_USAGE)
    backend_tree = str(resolve_tree(args.backend_tree)) if args.backend_tree else (str(tree) if tree else None)
    body = {"definition": args.definition, "tree": backend_tree, "key": args.key, "params": parse_params(args.param),
            "holder": holder_name(args, tree), "instance": optional_instance(args.instance_owner, tree),
            "project": args.project, "session": args.session or (branch_of(tree) if args.project else None),
            "ttl": args.ttl, "idleTimeout": args.idle_timeout, "restart": args.restart, "wait": not args.no_wait}
    secrets = bool(args.secrets)
    with client_from_args(args) as client:
        response = client.post("/api/backend-leases", json=body,
                               params={"waitSeconds": 0 if args.no_wait else min(POLL_SLICE, args.wait),
                                       "secrets": str(secrets).lower()}, timeout=POLL_SLICE + 300)
        if not args.no_wait:
            response = wait_backend_lease(client, response, args.wait, args.quiet or args.json or args.shell,
                                          secrets)
            if response.get("status") == "starting":
                lease = response.get("lease") or {}
                fail(f"backend {lease.get('backendId')} not ready in {args.wait:.0f}s; lease {lease.get('id')} "
                     f"is still held", BACKEND_FAILED)
    return print_backend_lease(response, args)


def cmd_renew(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        response = client.post(f"/api/backend-leases/{quote(args.lease)}/renew",
                               json={"ttl": args.ttl} if args.ttl is not None else {})
    if args.json:
        print_json(response)
    else:
        lease = response.get("lease") or {}
        ok(f"lease {lease.get('id')} renewed; it expires in "
           f"{describe_duration(lease.get('expiresIn')) if lease.get('expiresIn') is not None else 'never'}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        response = client.post(f"/api/backend-leases/{quote(args.lease)}/release", json={"reason": args.reason})
    if args.json:
        print_json(response)
        return 0
    backend = response.get("backend") or {}
    left = len(backend.get("leases") or [])
    if left:
        ok(f"lease {args.lease} released; {backend.get('id')} keeps running for {left} other lease(s)")
    elif backend:
        timeout = backend.get("idleTimeout")
        ok(f"lease {args.lease} released; {backend.get('id')} has no lease left and stops "
           + ("never on its own" if timeout == "never" else f"after {describe_duration(timeout)} without one"))
    else:
        ok(f"lease {args.lease} released")
    return 0


def lease_rows(items: list[dict]) -> list[tuple]:
    rows = []
    for lease in items:
        expires = lease.get("expiresIn")
        rows.append((lease.get("id") or "-", lease.get("backendId"), lease.get("kind"), lease.get("holder"),
                     f"{lease.get('project') or '-'} / {lease.get('session') or '-'}",
                     lease.get("driverSid") or "-",
                     "never" if expires is None else describe_duration(max(0.0, float(expires)))))
    return rows


def cmd_leases(args: argparse.Namespace) -> int:
    params = {"backend": args.id, "instance": args.instance_owner}
    with client_from_args(args) as client:
        items = client.get("/api/backend-leases", params={k: v for k, v in params.items() if v})["items"]
    if args.json:
        print_json({"items": items})
        return 0
    console.print(table(("lease", "backend", "kind", "holder", "project / session", "driver", "expires in"),
                        lease_rows(items), title="Backend leases"))
    return 0


def render_info(found: dict, urls_only: bool = False) -> None:
    for name, url in (found.get("urls") or {}).items():
        write_line(f"  {name}: {url}")
    if urls_only:
        return
    for key in ("commit", "branch", "version", "worktree"):
        if found.get(key):
            write_line(f"  {key}: {found[key]}")
    for name, port in (found.get("ports") or {}).items():
        write_line(f"  port {name}: {port}")
    for name, value in (found.get("credentials") or {}).items():
        write_line(f"  credential {name}: {json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}")
    for name, value in (found.get("env") or {}).items():
        write_line(f"  {name}={value}")
    for name, value in (found.get("extra") or {}).items():
        write_line(f"  {name}: {json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}")


def cmd_info(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        if args.lease:
            found = client.get(f"/api/backend-leases/{quote(args.lease)}/info",
                               params={"secrets": str(bool(args.secrets)).lower()})
            lease = {"id": args.lease}
        else:
            if args.secrets:
                fail("secrets are only handed to a lease holder: pass the lease id (eks-harness backend info <lease> "
                     "--secrets)", EXIT_USAGE)
            _, _, backend_id = backend_target(args)
            record = client.get(backend_path(backend_id))
            found = record.get("info") or {}
            lease = {"backendId": backend_id}
    if args.json:
        print_json(found)
    elif args.shell:
        for name, value in info_exports(found, lease).items():
            print(shell_line(name, value))
    else:
        if found.get("url"):
            write_line(found["url"])
        render_info(found)
        if found.get("redacted"):
            info("credentials and secret values are redacted; a lease holder sees them with --secrets")
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


def idle_text(record: dict) -> str:
    timeout = record.get("idleTimeout")
    if record.get("leases"):
        return f"held; stops {describe_duration(timeout)} after the last lease" if timeout != "never" else "held"
    if timeout == "never":
        return "no lease; never stops on its own"
    stops = record.get("stopsAt")
    if stops:
        return f"no lease; stops in {describe_duration(max(0.0, float(stops) - time.time()))}"
    return f"stops {describe_duration(timeout)} after the last lease"


def dependency_text(record: dict) -> str:
    parts = [f"needs {backend}" for backend in (record.get("dependencies") or {}).values()]
    parts += [f"held by {backend}" for backend in record.get("dependents") or []]
    return ", ".join(parts) or "-"


def render_backend(record: dict) -> None:
    rows = [("id", record.get("id")), ("status", record.get("status")),
            ("definition", record.get("definition")), ("identity", record.get("instance")),
            ("generation", record.get("generation")),
            ("ports", ", ".join(f"{k}={v}" for k, v in (record.get("ports") or {}).items()) or "-"),
            ("leases", ", ".join(record.get("bindings") or []) or "-"), ("idle", idle_text(record)),
            ("processes", ", ".join(f"{name} ({'alive' if alive else 'dead'})"
                                    for name, alive in (record.get("alive") or {}).items()) or "-")]
    if record.get("dependencies"):
        rows.append(("depends on", ", ".join(f"{name} ({backend})"
                                             for name, backend in record["dependencies"].items())))
    if record.get("dependents"):
        rows.append(("held by", ", ".join(record["dependents"])))
    if record.get("error"):
        rows.append(("error", str(record["error"]).splitlines()[0]))
    console.print(table(("field", "value"), rows, title=f"Backend {record.get('id')}"))
    found = record.get("info") or {}
    if found.get("url"):
        write_line(found["url"])
    render_info(found)
    for name, value in (record.get("exports") or {}).items():
        if name not in (found.get("env") or {}):
            write_line(f"  {name}={value}")


def cmd_stop(args: argparse.Namespace) -> int:
    _, _, backend_id = backend_target(args)
    cleanup = bool(args.final or args.cleanup)
    with client_from_args(args) as client:
        record = client.post(backend_path(backend_id, "/stop"),
                             json={"cleanup": cleanup, "force": bool(args.force), "reason": "requested"}, timeout=1900)
    if args.json:
        print_json(record)
    else:
        ok(f"backend {backend_id} stopped and its leases ended" + (" (cleanup ran)" if cleanup else ""))
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
    rows = [(b.get("id"), b.get("status"), str(b.get("generation") or 0),
             ", ".join(f"{k}={v}" for k, v in (b.get("ports") or {}).items()) or "-",
             str(len(b.get("leases") or [])), dependency_text(b), idle_text(b)) for b in result.get("items") or []]
    console.print(table(("id", "status", "gen", "ports", "leases", "dependencies", "idle"), rows, title="Backends"))
    for b in result.get("items") or []:
        url = (b.get("info") or {}).get("url")
        if url:
            write_line(f"{b.get('id')}: {url}")
    return 0


def cmd_definitions(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.tree)
    with client_from_args(args) as client:
        result = client.get("/api/backends/definitions", params={"tree": str(tree) if tree else None})
    if args.json:
        print_json(result.get("items") or [])
        return 0
    console.print(table(("name", "source", "requires", "path"),
                        [(d.get("name"), d.get("source"),
                          ", ".join(item.get("name", "") for item in d.get("requires") or []) or "-", d.get("path"))
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


def add_target(parser: argparse.ArgumentParser, lease: bool = True) -> None:
    parser.add_argument("--definition", help="backend definition name (default: [backends] default in "
                                             ".harness/project.toml, or the only definition of the tree)")
    parser.add_argument("--id", help="backend id (default <definition>@tree:<tree>)")
    parser.add_argument("--key", help="identity of the instance instead of the work tree")
    parser.add_argument("--param", action="append", metavar="KEY=VALUE", help="instance parameter (repeatable)")
    parser.add_argument("--instance", help="explicit instance identity (older per-session backends)")
    parser.add_argument("--tree", help="work tree whose definitions are used (default: the current one)")
    if lease:
        parser.add_argument("--lease", help="the backend this backend lease holds")


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
    backend = subparsers.add_parser("backend", help="shared backends: leases, info, logs, seeding",
                                    description="A backend instance is shared: every holder takes a lease on it, "
                                                "the daemon starts it on the first lease, keeps it while any lease "
                                                "is held and stops it (running the project's cleanup) once no lease "
                                                "is left for its idle timeout.")
    sub = backend.add_subparsers(dest="backend_command", metavar="<action>", required=True)
    p = sub.add_parser("acquire", help="take a lease on a backend instance (starting it if needed) and wait for it")
    p.add_argument("--definition", help="backend definition (default: [backends] default in .harness/project.toml)")
    p.add_argument("--tree", help="work tree whose definitions are used (default: the current one)")
    p.add_argument("--backend-tree", help="the tree the backend runs from when it differs from --tree")
    p.add_argument("--key", help="identity of the instance instead of the backend tree")
    p.add_argument("--param", action="append", metavar="KEY=VALUE", help="instance parameter (repeatable)")
    p.add_argument("--holder", help="label shown for this lease (default: this harness instance)")
    p.add_argument("--instance", dest="instance_owner", help="harness instance that owns the lease")
    p.add_argument("--project", help="project the lease is for (needed without admin rights)")
    p.add_argument("--session", help="session the lease is for (default: the branch)")
    p.add_argument("--ttl", help="the lease expires this long after its last renewal (600, 15m, never)")
    p.add_argument("--idle-timeout", help="stop and clean up this long after the last lease (900, 15m, 0, never)")
    p.add_argument("--restart", action="store_true", help="restart the instance (every holder sees the new one)")
    p.add_argument("--wait", type=float, default=3600, help="seconds to wait until the backend is ready")
    p.add_argument("--no-wait", action="store_true", help="return the lease at once")
    p.add_argument("--secrets", action="store_true", help="also print credentials and secret values")
    p.add_argument("--quiet", action="store_true", help="do not stream prepare/process output while waiting")
    p.add_argument("--shell", action="store_true", help="print EKS_BACKEND_LEASE, EKS_BACKEND_URL and the env")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_acquire)
    p = sub.add_parser("renew", help="renew a backend lease (heartbeat)")
    p.add_argument("lease")
    p.add_argument("--ttl", help="new time to live (600, 15m, never)")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_renew)
    p = sub.add_parser("release", help="release a backend lease; the instance stops after its idle timeout")
    p.add_argument("lease")
    p.add_argument("--reason")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_release)
    p = sub.add_parser("leases", help="backend leases and their holders")
    p.add_argument("--id", help="only leases of this backend")
    p.add_argument("--instance", dest="instance_owner", help="only leases this harness instance owns")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_leases)
    p = sub.add_parser("info", help="what a backend exposes: URLs, ports, commit and branch, credentials")
    p.add_argument("lease", nargs="?", help="a backend lease id (needed for --secrets)")
    add_target(p, lease=False)
    p.add_argument("--secrets", action="store_true", help="show credentials and secret values (lease holders)")
    p.add_argument("--shell", action="store_true", help="print shell assignments")
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_info)
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
    p = sub.add_parser("stop", help="stop the backend now and end its leases")
    add_target(p)
    p.add_argument("--cleanup", action="store_true", help="also run the project's cleanup (drop its data)")
    p.add_argument("--force", action="store_true", help="also stop the backends that depend on this one")
    p.add_argument("--final", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", dest="json", action="store_true")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("hold", help="hold the backend with a time-limited lease (0 clears holds)")
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

