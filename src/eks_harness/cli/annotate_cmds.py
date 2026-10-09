from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from eks_harness.cli.client import HarnessClient, client_from_args
from eks_harness.cli.ui import (
    EXIT_NOTHING_SELECTED,
    EXIT_USAGE,
    console,
    fail,
    info,
    kv_panel,
    ok,
    print_json,
    table,
)


def _client(args: argparse.Namespace) -> HarnessClient:
    return client_from_args(args)


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON")


def _read_spec(path: str) -> Any:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        fail(f"{path} is not a file", EXIT_USAGE)
    text = file_path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _resolve_id(client: HarnessClient, artifact_id: str | None, project: str | None,
                session: str | None) -> str:
    if artifact_id:
        return artifact_id
    if not session:
        fail("pass an artifact id, or --session with --project", EXIT_USAGE)
    if not project:
        fail("pass --project owner/name with --session", EXIT_USAGE)
    page = client.get("/api/artifacts", params={"project": project, "session": session, "limit": 100})
    for item in page.get("items", []):
        if isinstance(item.get("meta"), dict) and "annotation" in item["meta"]:
            return item["id"]
    fail(f"no annotated artifact in {project} / {session}", EXIT_NOTHING_SELECTED)
    raise AssertionError("unreachable")


def _print_annotate(result: dict, args: argparse.Namespace) -> None:
    if args.json:
        print_json(result)
        return
    validation = result.get("validation", {})
    ok(f"Annotated version {result.get('version')} ({result.get('kind')})")
    rules = validation.get("rules", [])
    failed = [r for r in rules if not r.get("ok")]
    info(f"validation: {'ok' if validation.get('ok') else 'FAILED'} "
         f"({len(rules) - len(failed)}/{len(rules)} rules pass)")
    if validation.get("anchorFallback"):
        info("note: a coords anchor was used as a fallback")
    for crop in result.get("crops", []):
        sys.stdout.write(f"{crop.get('itemId')}: {crop.get('url')}\n")
    artifact = result.get("artifact", {})
    for key in ("rawUrl", "url"):
        if artifact.get(key):
            sys.stdout.write(f"{artifact[key]}\n")
    sys.stdout.flush()


def cmd_annotate(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"spec": _read_spec(args.spec)}
    if args.style:
        body["style"] = args.style
    if args.dry_run:
        body["dryRun"] = True
    with _client(args) as client:
        result = client.post(f"/api/artifacts/{quote(args.id, safe='')}/annotate", json=body, timeout=300)
    _print_annotate(result, args)
    return 0


def cmd_rerender(args: argparse.Namespace) -> int:
    with _client(args) as client:
        artifact_id = _resolve_id(client, args.id, args.project, args.session)
        body = {"style": args.style} if args.style else {}
        result = client.post(f"/api/artifacts/{quote(artifact_id, safe='')}/annotate/rerender", json=body,
                             timeout=300)
    _print_annotate(result, args)
    return 0


def cmd_recapture(args: argparse.Namespace) -> int:
    with _client(args) as client:
        artifact_id = _resolve_id(client, args.id, args.project, args.session)
        body = {"sid": args.sid} if args.sid else {}
        result = client.post(f"/api/artifacts/{quote(artifact_id, safe='')}/annotate/recapture", json=body,
                             timeout=900)
    _print_annotate(result, args)
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    with _client(args) as client:
        data = client.get(f"/api/artifacts/{quote(args.id, safe='')}/annotations")
    items = data.get("versions", [])
    if args.json:
        print_json(items)
        return 0
    if not items:
        info("No annotation versions yet.")
        return 0
    console.print(table(["Version", "Kind", "Style", "By", "Created", "File"],
                        [[v["version"], v.get("kind"), v.get("styleName") or "", v.get("createdBy") or "",
                          v.get("createdAt") or "", v.get("url")] for v in items]))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    with _client(args) as client:
        result = client.post(f"/api/artifacts/{quote(args.id, safe='')}/annotate/restore",
                             json={"version": args.version}, timeout=300)
    if args.json:
        print_json(result)
        return 0
    ok(f"Restored version {args.version} as version {result.get('version')}")
    return 0


def cmd_assets_add(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        fail(f"{path} is not a file", EXIT_USAGE)
    owner, sep, name = args.project.partition("/")
    if not sep or not owner or not name or "/" in name:
        fail(f"project must be owner/name, got {args.project!r}", EXIT_USAGE)
    fields = {"name": args.name} if args.name else None
    with _client(args) as client:
        asset = client.upload(f"/api/projects/{quote(owner, safe='')}/{quote(name, safe='')}/assets", [path],
                              fields, field_name="file", timeout=120)
    if args.json:
        print_json(asset)
    else:
        ok(f"Asset {asset['name']} added ({asset['filename']})")
        print(asset["url"])
    return 0


def cmd_assets_list(args: argparse.Namespace) -> int:
    owner, sep, name = args.project.partition("/")
    if not sep or not owner or not name:
        fail(f"project must be owner/name, got {args.project!r}", EXIT_USAGE)
    with _client(args) as client:
        data = client.get(f"/api/projects/{quote(owner)}/{quote(name)}/assets")
    items = data.get("items", [])
    if args.json:
        print_json(items)
        return 0
    if not items:
        info("No assets yet.")
        return 0
    console.print(table(["Name", "File", "Type", "Size", "Link"],
                        [[a["name"], a["filename"], a.get("mime"), a.get("size"), a.get("url")] for a in items]))
    return 0


def cmd_assets_rm(args: argparse.Namespace) -> int:
    owner, sep, name = args.project.partition("/")
    if not sep or not owner or not name:
        fail(f"project must be owner/name, got {args.project!r}", EXIT_USAGE)
    with _client(args) as client:
        result = client.delete(f"/api/projects/{quote(owner)}/{quote(name)}/assets/{quote(args.asset_name, safe='')}")
    if args.json:
        print_json(result)
    else:
        ok(f"Removed asset {args.asset_name}")
    return 0


ACTIONS = ("rerender", "recapture", "versions", "restore")


def register(subparsers: argparse._SubParsersAction) -> None:
    annotate = subparsers.add_parser(
        "annotate", help="render annotations onto captures (plus rerender, recapture, versions, restore)",
        description="Render an annotation spec onto a capture: eks-harness annotate <id> --spec <file>.\n\n"
                    "Rerender restyles without a device: annotate rerender <id|--session S> [--style Y].\n"
                    "Recapture replays the recipe with a lease: annotate recapture <id|--session S> [--sid SID].\n"
                    "Versions lists and restore rolls back: annotate versions <id>, "
                    "annotate restore <id> <version>.")
    annotate.add_argument("target", nargs="?", help="artifact id, or one of rerender, recapture, versions, restore")
    annotate.add_argument("extra", nargs="*", help="arguments of the action (artifact id, version number)")
    annotate.add_argument("--spec", help="spec file (.json, .yaml or .yml)")
    annotate.add_argument("--style", help="style name overriding the spec style")
    annotate.add_argument("--dry-run", action="store_true", help="validate and preview crops without storing")
    annotate.add_argument("--session", help="session name or slug holding the annotated artifact")
    annotate.add_argument("--project", help="project as owner/name (with --session)")
    annotate.add_argument("--sid", help="lease sid for the replay (recapture only)")
    _json_flag(annotate)
    annotate.set_defaults(func=_dispatch_annotate)

    assets = subparsers.add_parser("assets", help="manage project annotation assets (icons, images)")
    assets_sub = assets.add_subparsers(dest="assets_command", metavar="<action>", required=True)
    p = assets_sub.add_parser("add", help="upload a PNG, JPEG, WebP or SVG asset")
    p.add_argument("project", help="owner/name")
    p.add_argument("file", help="file to upload")
    p.add_argument("--name", help="asset name (default: from the file name)")
    _json_flag(p)
    p.set_defaults(func=cmd_assets_add)
    p = assets_sub.add_parser("list", help="list the assets of a project")
    p.add_argument("project", help="owner/name")
    _json_flag(p)
    p.set_defaults(func=cmd_assets_list)
    p = assets_sub.add_parser("rm", aliases=["remove"], help="remove an asset")
    p.add_argument("project", help="owner/name")
    p.add_argument("asset_name", metavar="name", help="asset name")
    _json_flag(p)
    p.set_defaults(func=cmd_assets_rm)


def _dispatch_annotate(args: argparse.Namespace) -> int:
    target = (getattr(args, "target", None) or "").strip()
    extra = list(getattr(args, "extra", None) or [])
    if target in ACTIONS:
        if target in ("rerender", "recapture"):
            args.id = extra[0] if extra else None
            if extra[1:]:
                fail(f"annotate {target} takes at most one artifact id", EXIT_USAGE)
            return cmd_rerender(args) if target == "rerender" else cmd_recapture(args)
        if target == "versions":
            if not extra:
                fail("pass an artifact id: eks-harness annotate versions <id>", EXIT_USAGE)
            args.id = extra[0]
            return cmd_versions(args)
        if len(extra) != 2:
            fail("pass an artifact id and a version: eks-harness annotate restore <id> <version>", EXIT_USAGE)
        args.id = extra[0]
        try:
            args.version = int(extra[1])
        except ValueError:
            fail(f"version must be a number, got {extra[1]!r}", EXIT_USAGE)
        return cmd_restore(args)
    if extra:
        fail(f"unexpected argument {extra[0]!r}: eks-harness annotate <id> --spec <file>", EXIT_USAGE)
    if not target:
        fail("pass an artifact id: eks-harness annotate <id> --spec <file>", EXIT_USAGE)
    if not getattr(args, "spec", None):
        fail("pass --spec <file.json|yaml>", EXIT_USAGE)
    args.id = target
    return cmd_annotate(args)


def annotate_panel(result: dict) -> None:
    console.print(kv_panel(f"annotation version {result.get('version')}", [
        ("Kind", result.get("kind")), ("Valid", result.get("validation", {}).get("ok")),
        ("Crops", len(result.get("crops", [])))]))
