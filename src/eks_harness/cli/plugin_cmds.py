from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from eks_harness.cli.client import client_from_args
from eks_harness.cli.lease_cmds import resolve_tree
from eks_harness.cli.ui import EXIT_ERROR, EXIT_USAGE, console, fail, kv_panel, ok, print_json, table, warn
from eks_harness.plugins import ManifestError, load_manifest
from eks_harness.plugins.discovery import plugin_dirs
from eks_harness.plugins.scaffold import scaffold, template_kinds
from eks_harness.plugins.trust import content_hash

STATE_STYLE = {"active": "green", "pending": "yellow", "changed": "yellow", "disabled": "dim", "error": "red",
               "shadowed": "dim"}


def _tree_param(args: argparse.Namespace) -> dict:
    tree = resolve_tree(args.tree) if getattr(args, "tree", None) is not None or not args.no_tree else None
    return {"tree": str(tree)} if tree else {}


def cmd_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        data = client.get("/api/plugins", params=_tree_param(args))
    items = [i for i in data["items"] if args.all or i["state"] != "shadowed"]
    if args.json:
        print_json({**data, "items": items})
        return 0
    rows = []
    for item in items:
        state = item["state"]
        contributes = ", ".join(f"{k} {v}" if v > 1 else k for k, v in (item.get("contributes") or {}).items())
        rows.append((item["id"], item.get("version", ""), item["kind"],
                     f"[{STATE_STYLE.get(state, '')}]{state}[/]", contributes or item.get("error", "")))
    console.print(table(["plugin", "version", "source", "state", "contributes"], rows))
    pending = [i["id"] for i in items if i["state"] in ("pending", "changed")]
    if pending:
        warn("waiting for approval: " + ", ".join(pending) + " (eks-harness plugin trust <id>)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        data = client.get(f"/api/plugins/{args.id}", params=_tree_param(args))
    if args.json:
        print_json(data)
        return 0
    console.print(kv_panel(data["id"], [("name", data.get("name")), ("version", data.get("version")),
                                        ("state", data["state"]), ("reason", data.get("reason")),
                                        ("source", data.get("origin")), ("root", data.get("root")),
                                        ("hash", data.get("hash"))]))
    contributions = data.get("contributions") or []
    if contributions:
        console.print(table(["type", "id", "entry / module / path"],
                            [(c["type"], c["id"], c.get("entry") or c.get("module") or c.get("path") or "")
                             for c in contributions]))
    values = data.get("values") or {}
    if values:
        console.print(table(["setting", "value"], [(k, str(v)) for k, v in values.items()]))
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        if args.revoke:
            data = client.delete(f"/api/plugins/{args.id}/trust", params=_tree_param(args))
        else:
            data = client.post(f"/api/plugins/{args.id}/trust", json=_tree_param(args))
    if args.json:
        print_json(data)
        return 0
    state = data["plugin"]["state"]
    ok(f"{args.id}: {'approval revoked' if args.revoke else 'approved'} (now {state})")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    source = args.source
    local = Path(source).expanduser()
    if local.exists():
        source = str(local.resolve())
    with client_from_args(args) as client:
        data = client.post("/api/plugins/install", json={"source": source, "trust": args.trust}, timeout=900)
    if args.json:
        print_json(data)
        return 0
    ok(f"added to {data['key']}: {data['value']}")
    for item in data["items"]:
        console.print(f"  {item['id']} {item.get('version', '')} {item['state']}", markup=False)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        data = client.post("/api/plugins/sync", json={**_tree_param(args), "update": args.update}, timeout=1800)
    if args.json:
        print_json(data)
        return 0
    failed = 0
    for item in data["items"]:
        if item["ok"]:
            ok(f"{item['spec']} -> {item['path']}")
        else:
            failed += 1
            warn(f"{item['spec']}: {item['error']}")
    return EXIT_ERROR if failed else 0


def cmd_set(args: argparse.Namespace) -> int:
    values = {}
    for item in args.values:
        key, sep, raw = item.partition("=")
        if not sep:
            fail(f"'{item}' is not key=value", EXIT_USAGE)
        try:
            values[key] = json.loads(raw)
        except ValueError:
            values[key] = raw
    with client_from_args(args) as client:
        data = client.put(f"/api/plugins/{args.id}/settings", json={"values": values})
    if args.json:
        print_json(data)
    else:
        ok(f"{args.id}: " + ", ".join(f"{k}={data['values'].get(k)!r}" for k in values))
    return 0


def validate_path(path: Path) -> list[dict]:
    results = []
    dirs = plugin_dirs(path)
    if not dirs:
        return [{"path": str(path), "ok": False, "error": "no harness-plugin.toml here"}]
    for root in dirs:
        try:
            manifest = load_manifest(root)
        except ManifestError as error:
            results.append({"path": str(root), "ok": False, "error": str(error)})
            continue
        problems = []
        for contribution in manifest.contributions:
            for key in ("module", "path"):
                rel = contribution.path if key == "path" else contribution.get("module")
                if rel and contribution.type != "ui" and not (root / rel).exists():
                    problems.append(f"{contribution.type} {contribution.id}: {rel} does not exist")
            if contribution.type == "ui" and not (root / contribution.get("module")).exists():
                problems.append(f"ui {contribution.id}: {contribution.get('module')} is not built yet")
        results.append({"path": str(root), "ok": not problems, "id": manifest.id, "version": manifest.version,
                        "hash": content_hash(root), "problems": problems,
                        "contributes": manifest.summary()["contributes"]})
    return results


def cmd_validate(args: argparse.Namespace) -> int:
    results = validate_path(Path(args.path).expanduser().resolve())
    if args.json:
        print_json({"items": results})
    else:
        for item in results:
            if item["ok"]:
                ok(f"{item['id']} {item['version']}: valid ({item['hash'][:19]})")
            elif "error" in item:
                warn(f"{item['path']}: {item['error']}")
            else:
                warn(f"{item['id']}: " + "; ".join(item["problems"]))
    return 0 if all(i["ok"] for i in results) else EXIT_ERROR


def cmd_new(args: argparse.Namespace) -> int:
    if args.kind == "list":
        for kind in template_kinds():
            console.print(kind, markup=False)
        return 0
    target = Path(args.path or args.id.rsplit(".", 1)[-1]).expanduser().resolve()
    try:
        written = scaffold(args.kind, target, args.id, name=args.name or "", description=args.description or "")
    except ValueError as error:
        fail(str(error), EXIT_USAGE)
    if args.json:
        print_json({"path": str(target), "files": [str(p) for p in written]})
        return 0
    ok(f"created {args.kind} plugin {args.id} in {target}")
    for path in written:
        console.print(f"  {path.relative_to(target)}", markup=False)
    console.print(f"next: eks-harness plugin dev {target}", markup=False)
    return 0


def cmd_dev(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    results = validate_path(root)
    if not all(r.get("id") for r in results):
        fail("; ".join(r.get("error", "") for r in results), EXIT_USAGE)
    with client_from_args(args) as client:
        client.post("/api/plugins/install", json={"source": str(root), "trust": True})
        ok(f"loaded {', '.join(r['id'] for r in results)}; watching {root} (Ctrl+C to stop)")
        last = {r["id"]: r["hash"] for r in results}
        while True:
            time.sleep(args.interval)
            current = {r["id"]: r.get("hash") for r in validate_path(root) if r.get("id")}
            if current == last:
                continue
            for plugin_id in current:
                client.post(f"/api/plugins/{plugin_id}/trust", json={})
            client.post("/api/plugins/refresh", json={})
            ok("reloaded " + ", ".join(current))
            last = current


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("plugin", help="list, trust, install, scaffold and develop plugins",
                                   description="Plugins add drivers, platforms, backends, seeders, logins, effects, "
                                               "transitions, renderers, scene engines, MCP tools, CLI commands, "
                                               "routes and UI modules.")
    sub = parser.add_subparsers(dest="plugin_command", metavar="<subcommand>", required=True)

    def tree_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--tree", help="work tree whose .harness/plugins and project.toml apply (default: cwd)")
        p.add_argument("--no-tree", action="store_true", help="only global plugins")
        p.add_argument("--json", action="store_true", help="print JSON")

    p = sub.add_parser("list", help="plugins and their state")
    tree_args(p)
    p.add_argument("--all", action="store_true", help="include plugins overridden by another of the same id")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("show", help="one plugin: contributions and settings")
    p.add_argument("id")
    tree_args(p)
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("trust", help="approve a folder or git plugin (pins its content hash)")
    p.add_argument("id")
    p.add_argument("--revoke", action="store_true", help="withdraw the approval")
    tree_args(p)
    p.set_defaults(func=cmd_trust)
    p = sub.add_parser("set", help="set plugin settings (hub-wide; a repo's project.toml can override them)")
    p.add_argument("id")
    p.add_argument("values", nargs="+", metavar="KEY=VALUE", help="JSON values or plain text")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("install", help="add a plugin folder or git plugin (url[#subpath][@ref], github:org/repo)")
    p.add_argument("source")
    p.add_argument("--trust", action="store_true", help="approve it right away")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("sync", help="fetch the configured git plugins")
    p.add_argument("--update", action="store_true", help="fetch again even when cached")
    tree_args(p)
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("validate", help="check plugin manifests in a folder (no daemon needed)")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("new", help="scaffold a plugin from a template ('new list' shows the kinds)")
    p.add_argument("kind", choices=[*template_kinds(), "list"])
    p.add_argument("id", nargs="?", default="acme.example", help="plugin id, for example acme.backend")
    p.add_argument("path", nargs="?", help="target folder (default: last part of the id)")
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_new)
    p = sub.add_parser("dev", help="load a plugin folder and reload it on every change")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(func=cmd_dev)
