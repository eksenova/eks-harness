from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path
from typing import Any

from eks_harness.cli.client import client_from_args
from eks_harness.cli.ui import EXIT_ERROR, EXIT_USAGE, console, fail, ok, print_json, table, warn
from eks_harness.paths import resolve_paths

ARTIFACT_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
STATUS_STYLE = {"pass": "green", "fail": "red", "error": "red", "skip": "dim"}


def add_arguments(sub: argparse._SubParsersAction) -> None:
    sheet = sub.add_parser("sheet", help="contact sheet of a video: one image with timed frames and the audio strip",
                           description="Builds a contact sheet (a grid of evenly spaced, frame-labelled tiles, "
                                       "highlighted cue frames and an audio waveform with marker ticks) from an "
                                       "artifact id or a local video and stores it next to the video.")
    sheet.add_argument("source", help="artifact id of a video, or a local video file")
    sheet.add_argument("--markers", type=Path, help="JSON: {beats, downbeats, cues, markers, frames} or a list")
    sheet.add_argument("--frames", type=int, help="evenly spaced frames (default: 2 per second, 12 to 96)")
    sheet.add_argument("--at", action="append", default=[], metavar="T[:LABEL]",
                       help="extra highlighted frame at T seconds (repeatable)")
    sheet.add_argument("--max-edge", type=int, default=2000, help="longest sheet edge in pixels (default 2000)")
    sheet.add_argument("--out", type=Path, help="folder for the local copies (default: the harness cache)")
    sheet.add_argument("--keep", action="store_true", help="keep earlier sheets of the video")
    _target_args(sheet)
    sheet.set_defaults(review=cmd_sheet)

    check = sub.add_parser("check", help="machine checks of a video against an expected timeline",
                           description="Runs timed checks (cut, motion, text, visual, black, frozen, safe_area, "
                                       "loudness, onset, beats and plugin checks) and prints measured vs expected "
                                       "times with the delta in frames. Exit code 1 when a check fails.")
    check.add_argument("source", nargs="?", help="artifact id of a video, or a local video file")
    check.add_argument("expectations", nargs="?", type=Path, help="JSON: {tolerance_frames, checks: [...]}")
    check.add_argument("--list", action="store_true", help="list the known check kinds")
    check.add_argument("--keep", action="store_true", help="keep earlier check reports of the video")
    check.add_argument("--tree", type=Path, help="work tree whose plugins add checks (default: cwd)")
    _target_args(check)
    check.set_defaults(review=cmd_check)


def _target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sid", help="for a local file: upload the video under this lease's session first")
    parser.add_argument("--project", help="for a local file: upload the video to this project first (owner/name)")
    parser.add_argument("--session", help="session for --project")
    parser.add_argument("--local", action="store_true", help="work on a local file only, store nothing")
    parser.add_argument("--json", action="store_true", help="print JSON")


def _is_artifact(source: str) -> bool:
    return bool(ARTIFACT_ID.match(source)) and not Path(source).exists()


def _target(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.sid:
        return {"sid": args.sid}
    if args.project:
        return {"project": args.project, "session": args.session}
    return None


def _upload_video(client: Any, path: Path, target: dict) -> str:
    fields = {**target, "kind": "video", "caption": f"Reviewed video {path.name}", "source": "cli"}
    data = client.upload("/api/artifacts", [path], {k: v for k, v in fields.items() if v is not None})
    return data["id"]


def _resolve(args: argparse.Namespace, client_factory) -> tuple[str | None, Path | None]:
    source = args.source
    if _is_artifact(source):
        return source.upper(), None
    path = Path(source).expanduser()
    if not path.is_file():
        fail(f"{source} is neither an artifact id nor a file", EXIT_USAGE)
    target = None if args.local else _target(args)
    if target is None:
        return None, path.resolve()
    with client_factory() as client:
        return _upload_video(client, path, target), path.resolve()


def _cache(name: str) -> Path:
    folder = resolve_paths().cache_dir / "review" / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def cmd_sheet(args: argparse.Namespace) -> int:
    from eks_harness.review import load_markers, make_sheets, parse_at, parse_markers

    try:
        markers = load_markers(args.markers) if args.markers else parse_markers(None)
        extra = parse_at(args.at)
    except (OSError, ValueError) as error:
        fail(f"markers: {error}", EXIT_USAGE)
    markers.frames.extend(extra)
    artifact_id, path = _resolve(args, lambda: client_from_args(args))
    if artifact_id is None:
        out = args.out or _cache(path.stem)
        result = make_sheets(path, out, frames=args.frames, markers=markers, max_edge=args.max_edge)
        if args.json:
            print_json(result.as_dict())
            return 0
        for sheet in result.sheets:
            print(f"sheet {sheet.part}/{sheet.parts} ({sheet.start:.2f}-{sheet.end:.2f}s, {len(sheet.tiles)} frames)")
            print(f"  local: {sheet.path}")
        if not args.local:
            warn("stored nowhere: pass an artifact id, or --sid / --project to keep it in the harness")
        return 0
    body = {"markers": markers.as_dict(), "frames": args.frames, "maxEdge": args.max_edge, "replace": not args.keep}
    from eks_harness.flows.client import download

    with client_from_args(args) as client:
        data = client.post(f"/api/artifacts/{artifact_id}/sheet", json=body, timeout=900)
        found = download(client, [s["id"] for s in data["sheets"]], args.out or _cache(artifact_id))
    for sheet in data["sheets"]:
        sheet["local"] = str(found.get(sheet["id"])) if found.get(sheet["id"]) else None
    if args.json:
        print_json(data)
        return 0
    for sheet in data["sheets"]:
        lo, hi = sheet["range"]
        print(f"sheet {sheet['part']}/{sheet['parts']} ({lo:.2f}-{hi:.2f}s, {len(sheet['tiles'])} frames)")
        print(f"  page: {sheet['url']}")
        if sheet.get("local"):
            print(f"  local: {sheet['local']}")
    print(f"video: {data['video']['url']}")
    return 0


def inline_templates(raw: Any, base: Path) -> Any:
    items = raw.get("checks", raw.get("expectations")) if isinstance(raw, dict) else raw
    for item in items or []:
        params = item.get("params") if isinstance(item, dict) else None
        if not isinstance(params, dict) or not params.get("template") or params.get("template_b64"):
            continue
        file = Path(params["template"]).expanduser()
        file = file if file.is_absolute() else base / file
        if not file.is_file():
            fail(f"template {file} does not exist", EXIT_USAGE)
        params["template_b64"] = base64.b64encode(file.read_bytes()).decode()
        params.pop("template")
    return raw


def print_report(report: dict, text: str | None = None) -> None:
    rows = []
    for r in report["results"]:
        expected = "" if r["expected"] is None else f"{r['expected']:.3f}"
        measured = "" if r["measured"] is None else f"{r['measured']:.3f}"
        delta = "" if r["deltaFrames"] is None else f"{r['deltaFrames']:+d} / {r['toleranceFrames']}"
        status = r["status"]
        rows.append((str(r["index"]), f"[{STATUS_STYLE.get(status, '')}]{status}[/]", f"{r['kind']} {r['label']}".strip(),
                     expected, measured, delta, r["detail"]))
    console.print(table(["#", "status", "check", "expected s", "measured s", "delta f / tol", "detail"], rows))
    counts = ", ".join(f"{n} {s}" for s, n in report["counts"].items() if n)
    (ok if report["ok"] else warn)(f"{'PASS' if report['ok'] else 'FAIL'}: {counts}")


def cmd_check(args: argparse.Namespace) -> int:
    from eks_harness.review import ExpectationError, kinds, parse_expectations, run_checks
    from eks_harness.review.checks import load_plugin_checks

    if args.list:
        load_plugin_checks(None, args.tree)
        names = kinds()
        if args.json:
            print_json({"items": names})
        else:
            console.print("\n".join(names), markup=False)
        return 0
    if not args.source or not args.expectations:
        fail("pass a video (artifact id or file) and an expectations file", EXIT_USAGE)
    try:
        raw = json.loads(args.expectations.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        fail(f"{args.expectations}: {error}", EXIT_USAGE)
    base = args.expectations.resolve().parent
    artifact_id, path = _resolve(args, lambda: client_from_args(args))
    if artifact_id is None:
        try:
            report = run_checks(path, parse_expectations(raw, base=base), base=base)
        except ExpectationError as error:
            fail(str(error), EXIT_USAGE)
        data = report.as_dict()
        if args.json:
            print_json(data)
        elif console.is_terminal:
            print_report(data)
        else:
            print(report.text())
        return 0 if report.ok else EXIT_ERROR
    body = {"expectations": inline_templates(raw, base), "replace": not args.keep}
    tree = args.tree or Path.cwd()
    body["tree"] = str(tree.resolve())
    with client_from_args(args) as client:
        data = client.post(f"/api/artifacts/{artifact_id}/checks", json=body, timeout=1800)
    if args.json:
        print_json(data)
    else:
        if console.is_terminal:
            print_report(data["report"])
        else:
            print(data["text"])
        print(f"report: {data['artifact']['url']}")
    return 0 if data["ok"] else EXIT_ERROR
