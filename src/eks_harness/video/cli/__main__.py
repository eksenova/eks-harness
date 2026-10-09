"""Video engine commands (``eks-harness video ...``).

Subcommands:

* ``render`` - execute a project.py and render to mp4.
* ``validate`` - load a project.py and run the IR validator.
* ``schema`` - write the master JSON Schema to a file.
* ``sync`` - load project.py and write project.json snapshot next to it.
* ``doctor`` - diagnose ffmpeg, encoders, and dependency availability.
* ``init`` - scaffold a project from a cookbook template.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eks_harness.video import Project
from eks_harness.video import schema as schema_module
from eks_harness.video.cli.doctor import run_doctor
from eks_harness.video.cli.init import run_init
from eks_harness.video.cli.parser import configure
from eks_harness.video.render import Renderer, RenderOptions

_LOG = logging.getLogger("eks_harness.video")


HANDLERS: dict[str, Any] = {}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run(args)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    handler = {
        "render": _cmd_render,
        "validate": _cmd_validate,
        "schema": _cmd_schema,
        "sync": _cmd_sync,
        "doctor": _cmd_doctor,
        "init": _cmd_init,
    }[args.video_command]
    return handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eks-harness video", description="Video engine commands")
    return configure(parser)


def _cmd_render(args: argparse.Namespace) -> int:
    project = _load_project(args.project)
    out_path = args.out or args.project.with_suffix(".mp4")
    range_start, range_end = _parse_range(args.range_spec)
    options = RenderOptions(
        output=out_path,
        mode=args.mode,
        range_start=range_start,
        range_end=range_end,
        ffmpeg_binary=args.ffmpeg,
    )
    renderer = Renderer(project, options)
    final = renderer.render(out_path)
    print(str(final))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        _load_project(args.project)
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    out = schema_module.export_to_file(args.out)
    print(str(out))
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    project_py = project_dir / "project.py"
    if not project_py.exists():
        print(f"no project.py at {project_py}", file=sys.stderr)
        return 1
    project = _load_project(project_py)
    snapshot_path = project_dir / "project.json"
    snapshot_path.write_text(
        project.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
    print(str(snapshot_path))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    return run_doctor(ffmpeg_binary=args.ffmpeg)


def _cmd_init(args: argparse.Namespace) -> int:
    return run_init(
        template_name=args.template,
        dest_dir=args.dest_dir,
        force=args.force,
        list_only=args.list_templates,
    )


def _parse_range(spec: str | None) -> tuple[float | None, float | None]:
    if spec is None:
        return None, None
    if ":" not in spec:
        raise SystemExit("--range must be START:END (seconds)")
    a, b = spec.split(":", 1)
    return float(a), float(b)


def _load_project(path: Path) -> Project:
    from eks_harness.video.loader import load_project_file

    project: Any = load_project_file(path)
    if not isinstance(project, Project):
        raise RuntimeError(f"{path}: `project` must be an eks_harness.video.Project instance, got {type(project).__name__}")
    return project


if __name__ == "__main__":
    raise SystemExit(main())
