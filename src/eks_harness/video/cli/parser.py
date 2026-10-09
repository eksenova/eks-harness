from __future__ import annotations

import argparse
from pathlib import Path


def configure(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="video_command", required=True, metavar="<command>")

    render = sub.add_parser("render", help="Render a project.py to an mp4 file")
    render.add_argument("project", type=Path)
    render.add_argument("--mode", choices=["preview", "final"], default="final")
    render.add_argument("--out", type=Path, default=None)
    render.add_argument("--range", dest="range_spec", default=None)
    render.add_argument("--ffmpeg", default="ffmpeg")

    validate = sub.add_parser("validate", help="Validate a project.py without rendering")
    validate.add_argument("project", type=Path)

    schema = sub.add_parser("schema", help="Export the master JSON Schema")
    schema.add_argument("--out", type=Path, default=Path("schemas/master.schema.json"))

    sync = sub.add_parser("sync", help="Snapshot project.py to project.json")
    sync.add_argument("project_dir", type=Path)

    doctor = sub.add_parser(
        "doctor",
        help="Diagnose ffmpeg, encoder availability, and Python dependencies",
    )
    doctor.add_argument("--ffmpeg", default="ffmpeg")

    from eks_harness.cli.review_cmds import add_arguments

    add_arguments(sub)

    init = sub.add_parser(
        "init",
        help="Scaffold a project from a cookbook template",
    )
    init.add_argument("template", nargs="?", default=None)
    init.add_argument("dest_dir", nargs="?", type=Path, default=None)
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing project.py at the destination",
    )
    init.add_argument(
        "--list",
        dest="list_templates",
        action="store_true",
        help="List the available templates and exit",
    )

    return parser
