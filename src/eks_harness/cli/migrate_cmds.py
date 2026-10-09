from __future__ import annotations

import argparse
import json
import shutil
import tomllib
from pathlib import Path
from typing import Any

from eks_harness.cli.client import client_from_args
from eks_harness.cli.ui import EXIT_ERROR, EXIT_USAGE, console, fail, ok, print_json, table, warn
from eks_harness.config import SETTINGS, ConfigError, coerce, parse_cli_value, read_file
from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths


def cmd_config(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser()
    data = read_file(source)
    if not data:
        fail(f"{source} holds no settings", EXIT_USAGE)
    overrides = dict(item.split("=", 1) for item in args.set or [])
    config = load_config(resolve_paths().ensure())
    applied, skipped = [], []
    for key, value in {**data, **overrides}.items():
        if key not in SETTINGS:
            skipped.append((key, "unknown in this version"))
            continue
        try:
            new = parse_cli_value(key, value) if key in overrides else coerce(key, value)
        except (ConfigError, ValueError) as error:
            skipped.append((key, str(error)))
            continue
        applied.append((key, new))
    if not args.dry_run:
        config.set_many(dict(applied))
    if args.json:
        print_json({"applied": dict(applied), "skipped": dict(skipped), "dryRun": args.dry_run})
        return 0
    console.print(table(["setting", "value"], [(k, json.dumps(v)) for k, v in applied]))
    for key, reason in skipped:
        warn(f"skipped {key}: {reason}")
    ok(("would apply" if args.dry_run else "applied") + f" {len(applied)} settings")
    return 0


def farm_nodes(source: Path) -> list[dict[str, Any]]:
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    nodes = []
    for remote in data.get("remote") or []:
        per_slot = int(remote.get("workers_per_slot") or 1)
        slots = []
        for index, slot in enumerate(remote.get("slots") or [{}]):
            env = {k: str(v) for k, v in {**(remote.get("env") or {}), **(slot.get("env") or {})}.items()}
            backend = str(env.get("EKS_HARNESS_CYCLES_DEVICE") or env.get("VIDEODSL_CYCLES_DEVICE") or "")
            if "VIDEODSL_CYCLES_DEVICE" in env:
                env["EKS_HARNESS_CYCLES_DEVICE"] = env.pop("VIDEODSL_CYCLES_DEVICE")
            gpu = "CUDA_VISIBLE_DEVICES" in env or backend in ("CUDA", "OPTIX", "HIP", "METAL")
            slot_id = f"gpu{env.get('CUDA_VISIBLE_DEVICES', index)}" if gpu else f"cpu{index}"
            tags = ["gpu", backend.lower()] if gpu and backend else ["gpu"] if gpu else ["cpu"]
            slots.append({"id": slot_id, "workers": per_slot, "env": env, "tags": tags,
                          **({"backend": backend} if backend else {})})
        name = str(remote.get("name") or remote["host"]).lower().replace("_", "-").replace(".", "-")
        nodes.append({"id": name, "host": remote["host"], "slots": slots,
                      "blender": str((remote.get("tools") or {}).get("blender") or ""),
                      "wsl": bool(remote.get("wsl"))})
    return nodes


def cmd_farm(args: argparse.Namespace) -> int:
    from eks_harness.nodes.install import InstallError, RemoteTarget, install_node

    source = Path(args.source).expanduser()
    if not source.is_file():
        fail(f"no farm config at {source}", EXIT_USAGE)
    nodes = farm_nodes(source)
    if not args.apply or args.dry_run:
        if args.json:
            print_json({"nodes": nodes})
        else:
            console.print(table(["node", "ssh", "slots", "blender"],
                                [(n["id"], n["host"], ", ".join(f"{s['id']} x{s['workers']}" for s in n["slots"]),
                                  n["blender"]) for n in nodes]))
            ok("rerun with --apply to register and install them")
        return 0
    failed = 0
    with client_from_args(args) as client:
        existing = {n["id"] for n in client.get("/api/nodes")["items"]}
        for node in nodes:
            data = client.post(f"/api/nodes/{node['id']}/token") if node["id"] in existing else \
                client.post("/api/nodes", json={"id": node["id"], "label": node["host"],
                                                "config": {"slots": node["slots"]}})
            if node["id"] in existing:
                client.patch(f"/api/nodes/{node['id']}", json={"config": {"slots": node["slots"]}})
            try:
                install_node(RemoteTarget(node["host"], wsl=node["wsl"] or args.wsl == node["id"]),
                             hub=args.hub_url or data["hubUrl"], token=data["token"], name=node["id"],
                             slots=node["slots"], blender=node["blender"],
                             log=lambda text, n=node["id"]: console.print(f"  {n}: {text}", markup=False))
                ok(f"node {node['id']} installed")
            except InstallError as error:
                failed += 1
                warn(f"node {node['id']}: {error}")
    target = resolve_paths().ensure().config_dir / "farm.toml"
    if not args.keep_farm:
        if target.exists() or source == target:
            shutil.copyfile(source, source.with_suffix(".toml.pre-nodes"))
        target.write_text("[local]\nenabled = true\n\n[nodes]\nenabled = true\n", encoding="utf-8")
        ok(f"wrote {target}: local plus every online node with Blender")
    return EXIT_ERROR if failed else 0


def studio_jobs(source: Path) -> list[dict[str, Any]]:
    file = source / "jobs.json" if source.is_dir() else source
    data = json.loads(file.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [j for j in jobs or [] if j.get("status") == "succeeded" and j.get("output_path")
            and Path(j["output_path"]).is_file()]


def cmd_studio(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser()
    if not source.exists():
        fail(f"{source} does not exist", EXIT_USAGE)
    jobs = studio_jobs(source)
    rows = []
    with client_from_args(args) as client:
        for job in jobs:
            project = args.project or f"video/{job.get('project_id') or 'renders'}"
            session = args.session or str(job.get("project_id") or "renders")
            if args.dry_run:
                rows.append((job["job_id"], job["output_path"], project, session, "would upload"))
                continue
            result = client.upload("/api/artifacts", [Path(job["output_path"])],
                                   fields={"project": project, "session": session, "kind": "video",
                                           "caption": f"{job.get('mode', 'render')} render {job['job_id']}",
                                           "tags": ["render", "studio"]})
            rows.append((job["job_id"], job["output_path"], project, session,
                         (result.get("url") if isinstance(result, dict) else "uploaded") or "uploaded"))
    if args.json:
        print_json({"items": [dict(zip(("job", "path", "project", "session", "result"), r, strict=True))
                              for r in rows]})
    else:
        console.print(table(["job", "file", "project", "session", "result"], rows))
        ok(f"{len(rows)} studio renders {'found' if args.dry_run else 'imported'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    paths = resolve_paths()
    report = []
    for label, path in (("config", paths.config_dir), ("data", paths.data_dir), ("cache", paths.cache_dir),
                        ("state", paths.state_dir), ("log", paths.log_dir), ("database", paths.db_file),
                        ("store", paths.store_dir)):
        report.append({"what": label, "path": str(path), "exists": path.exists(),
                       "size": _size(path) if path.exists() and args.sizes else None})
    for extra in args.also or []:
        path = Path(extra).expanduser()
        report.append({"what": "extra", "path": str(path), "exists": path.exists(),
                       "size": _size(path) if path.exists() and args.sizes else None})
    if args.json:
        print_json({"items": report})
        return 0
    console.print(table(["what", "path", "exists", "size"],
                        [(r["what"], r["path"], "yes" if r["exists"] else "no",
                          f"{r['size'] / 2**20:.0f} MB" if r["size"] is not None else "") for r in report]))
    return 0


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("migrate", help="bring settings, render farms and studio renders over",
                                   description="Import settings from another config.json, turn an SSH render farm "
                                               "into nodes and import studio renders as artifacts.")
    sub = parser.add_subparsers(dest="migrate_command", metavar="<subcommand>", required=True)
    p = sub.add_parser("check", help="where this installation keeps its data, and how big it is")
    p.add_argument("--sizes", action="store_true")
    p.add_argument("--also", action="append", help="another folder to report (repeatable)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("config", help="merge settings from another config.json (unknown keys are skipped)")
    p.add_argument("source")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="extra settings to apply (JSON values)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config)
    p = sub.add_parser("farm", help="turn a farm.toml's SSH hosts into nodes")
    p.add_argument("source", help="farm.toml with [[remote]] hosts")
    p.add_argument("--apply", action="store_true", help="register and install the nodes over SSH")
    p.add_argument("--hub-url", help="URL the nodes dial")
    p.add_argument("--wsl", help="node id whose SSH host is Windows (install inside WSL)")
    p.add_argument("--keep-farm", action="store_true", help="do not rewrite the hub's farm.toml")
    p.add_argument("--dry-run", action="store_true", help="only show the nodes (the default without --apply)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_farm)
    p = sub.add_parser("studio", help="import finished studio renders (jobs.json) as video artifacts")
    p.add_argument("source", help="the old studio state folder or its jobs.json")
    p.add_argument("--project", help="project for the renders (default video/<studio project>)")
    p.add_argument("--session", help="session (default: the studio project)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_studio)
