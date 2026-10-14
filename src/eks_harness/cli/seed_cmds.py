from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import quote

from eks_harness.cli.client import client_from_args
from eks_harness.cli.ui import (
    confirm_or_exit,
    console,
    ok,
    print_json,
    table,
    write_line,
)


def register(subparsers: argparse._SubParsersAction) -> None:
    seed = subparsers.add_parser("seed", help="seed snapshots kept by the hub",
                                 description="Snapshots the seed engine built: their keys, personas and determinism. "
                                             "Build a seed with eks-harness run <file>::<seed>.")
    sub = seed.add_subparsers(dest="seed_command", metavar="<command>")
    listing = sub.add_parser("snapshots", help="seed snapshots, newest first")
    listing.add_argument("--definition", help="only snapshots of this backend definition")
    listing.add_argument("--json", action="store_true", help="print JSON")
    listing.set_defaults(func=cmd_snapshots)
    show = sub.add_parser("show", help="one snapshot: resources, personas (without secrets) and determinism")
    show.add_argument("key", help="snapshot key or a prefix of its hex digest")
    show.add_argument("--json", action="store_true", help="print JSON")
    show.set_defaults(func=cmd_show)
    drop = sub.add_parser("drop", help="delete a snapshot and its manifest")
    drop.add_argument("key", help="snapshot key or a prefix of its hex digest")
    drop.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    drop.add_argument("--json", action="store_true", help="print JSON")
    drop.set_defaults(func=cmd_drop)
    seed.set_defaults(func=lambda args: (seed.print_help(), 0)[1])


def short(key: str) -> str:
    return key.removeprefix("sha256:")[:12]


def determinism(item: dict[str, Any]) -> str:
    value = item.get("deterministic")
    if value is None:
        return "built once"
    if value:
        return "same as the last build"
    paths = (item.get("divergence") or {}).get("paths") or []
    return f"differs: {', '.join(paths[:3])}" + (" ..." if len(paths) > 3 else "")


def cmd_snapshots(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        result = client.get("/api/seeds", params={"definition": args.definition})
    items = result.get("items") or []
    if args.json:
        print_json(items)
        return 0
    rows = [(short(item["key"]), item["seed"], item["definition"], str(item.get("resources") or 0),
             ", ".join(item.get("personas") or []) or "-", determinism(item), item.get("created") or "")
            for item in items]
    console.print(table(("key", "seed", "backend", "resources", "personas", "determinism", "built"), rows,
                        title="Seed snapshots"))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        item = client.get(f"/api/seeds/{quote(args.key, safe='')}")
    if args.json:
        print_json(item)
        return 0
    rows = [("key", item["key"]), ("seed", item["seed"]), ("backend", item["definition"]),
            ("migrations", item.get("migrations") or "-"), ("snapshot", item.get("snapshot") or "-"),
            ("digest", item.get("digest") or "-"), ("outputs digest", item.get("outputsDigest")),
            ("bases", ", ".join(short(key) for key in item.get("bases") or []) or "-"),
            ("replayed after restore", ", ".join(item.get("replay") or []) or "-"),
            ("determinism", determinism(item)), ("built", f"{item.get('created')} in {item.get('buildMs')} ms")]
    console.print(table(("field", "value"), rows, title=f"Seed {item['seed']}"))
    personas = item.get("personaDetail") or {}
    if personas:
        console.print(table(("persona", "detail"), [(key, ", ".join(f"{name}={value}" for name, value in entry.items()))
                                                    for key, entry in personas.items()], title="Personas"))
    for path in sorted(item.get("outputs") or {}):
        write_line(path)
    return 0


def cmd_drop(args: argparse.Namespace) -> int:
    confirm_or_exit(f"drop seed snapshot {args.key}", args.yes)
    with client_from_args(args) as client:
        result = client.delete(f"/api/seeds/{quote(args.key, safe='')}")
    if args.json:
        print_json(result)
        return 0
    ok(f"seed snapshot {short(result['dropped'])} ({result['seed']}) dropped")
    if result.get("note"):
        write_line(result["note"])
    return 0
