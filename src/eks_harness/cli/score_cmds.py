from __future__ import annotations

import argparse
import json
from pathlib import Path

from eks_harness.cli.ui import EXIT_ERROR, EXIT_USAGE, console, fail, ok, print_json, table, warn

TEMPLATE = '''from eks_harness.score import Score, beats, cue

score = Score(fps=30, song="music/song.ogg", window=(0.0, 15.0), size=(1080, 1920), name="{name}")

cards = score.web("scenes/cards/index.html", id="cards")
phone = score.blender("scenes/phone.py", id="phone", textures={{"screen": cards}})

score.on(beats("downbeat")[0:8], cards.emit("flip"), id="flip-on-downbeats")
score.on(cards.event("flipped"), phone.emit("impact"), id="cards-to-phone")

score.edit.layer(phone).layer(cards)
'''


def _load(path: str):
    from eks_harness.score.loader import load_any

    file = Path(path).expanduser().resolve()
    if not file.is_file():
        fail(f"no score file {file}", EXIT_USAGE)
    try:
        return load_any(file), file.parent
    except Exception as error:
        fail(f"{file}: {error}", EXIT_USAGE)


def cmd_new(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser()
    if target.exists() and not args.force:
        fail(f"{target} exists (pass --force to overwrite)", EXIT_USAGE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(TEMPLATE.format(name=args.name or target.stem), encoding="utf-8")
    ok(f"wrote {target}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    score, _ = _load(args.path)
    summary = {"name": score.name, "fps": score.clock.fps, "tracks": [{"id": t.id, "kind": t.kind} for t in score.tracks],
               "rules": len(score.rules)}
    if args.json:
        print_json(summary)
    else:
        ok(f"{score.name}: {len(score.tracks)} tracks, {len(score.rules)} rules at {score.clock.fps:g} fps")
    return 0


def _events(text: str | None):
    from eks_harness.score import Event

    if not text:
        return []
    raw = json.loads(Path(text).read_text(encoding="utf-8")) if Path(text).is_file() else json.loads(text)
    return [Event(float(e["time"]), str(e["source"]), str(e["name"]), dict(e.get("data") or {})) for e in raw]


def cmd_plan(args: argparse.Namespace) -> int:
    from eks_harness.score import plan
    from eks_harness.score.analysis import beat_analyzer

    score, base = _load(args.path)
    result = plan(score, observed=_events(args.events), base=base, analyzer=None if args.no_analysis else beat_analyzer)
    data = result.as_dict()
    if args.json:
        print_json(data)
        return 0
    console.print(f"{len(data['beats'])} beats, {len(data['downbeats'])} downbeats, duration "
                  f"{data['duration'] or 0:.2f}s at {data['fps']:g} fps", markup=False)
    console.print(table(["frame", "time", "target", "verb", "rule", "cause"],
                        [(str(a["frame"]), f"{a['time']:.3f}", a["target"], a["verb"], a["rule"],
                          f"{a['cause']['source']}:{a['cause']['name']}" if a.get("cause") else "")
                         for a in data["actions"][: args.limit]]))
    for problem in data["warnings"]:
        warn(problem)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    score, _ = _load(args.path)
    target = Path(args.out).expanduser() if args.out else Path(args.path).with_suffix(".json")
    score.save(target)
    ok(f"wrote {target}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from eks_harness.score.analysis import beat_analyzer
    from eks_harness.score.render import ScoreRenderError, render_score

    score, base = _load(args.path)
    output = Path(args.out).expanduser().resolve() if args.out else base / "renders" / f"{score.name}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)

    def progress(stage: str, detail: dict) -> None:
        if not args.json:
            console.print(f"  {stage} " + " ".join(f"{k}={v}" for k, v in detail.items()), markup=False)

    try:
        result = render_score(score, base, None if args.scenes_only else output,
                              workspace=Path(args.workspace).expanduser() if args.workspace else None,
                              analyzer=None if args.no_analysis else beat_analyzer, progress=progress,
                              take_runner=_take_runner(), mode=args.mode)
    except ScoreRenderError as error:
        fail(str(error), EXIT_ERROR)
    if args.json:
        print_json(result.as_dict())
        return 0
    for problem in result.warnings:
        warn(problem)
    ok(f"rendered {result.output or 'scenes'} ({result.iterations} passes, {len(result.events)} events)")
    return 0


def _take_runner():
    try:
        from eks_harness.score.takes import device_take_runner
    except ImportError:
        return None
    return device_take_runner


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("score", help="synchronized scores: edit, Blender, web scenes and device takes",
                                   description="A score is one timeline (clock, beats, cues) that drives device "
                                               "takes, Blender scenes, web scenes and the video edit, with events "
                                               "flowing between them.")
    sub = parser.add_subparsers(dest="score_command", metavar="<subcommand>", required=True)
    p = sub.add_parser("new", help="write a starter score.py")
    p.add_argument("path", nargs="?", default="score.py")
    p.add_argument("--name")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_new)
    p = sub.add_parser("validate", help="load and validate a score (score.py or score.json)")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("plan", help="resolve beats, spans, events and every scheduled action with its frame")
    p.add_argument("path")
    p.add_argument("--events", help="extra observed events: JSON list or a file with one")
    p.add_argument("--no-analysis", action="store_true", help="do not analyze the song for beats")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("export", help="write the JSON IR of a score.py")
    p.add_argument("path")
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("render", help="run device takes, render scenes to a fixed point and composite the edit")
    p.add_argument("path")
    p.add_argument("--out")
    p.add_argument("--workspace", help="cache folder (default: <score dir>/.score-cache)")
    p.add_argument("--mode", choices=["preview", "final"], default="final")
    p.add_argument("--scenes-only", action="store_true", help="render the scene tracks without the composite")
    p.add_argument("--no-analysis", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_render)
