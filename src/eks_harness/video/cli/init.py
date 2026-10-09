"""``eks-harness video init`` - scaffold a project from a cookbook template.

Copies one of the cookbook recipes shipped in ``eks_harness.video.cookbook``
into a destination directory as ``project.py`` and creates a placeholder
``assets/`` directory.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


__all__ = ["COOKBOOK_DIR", "TEMPLATES", "available_templates", "run_init"]

COOKBOOK_DIR = Path(__file__).resolve().parents[1] / "cookbook"


TEMPLATES: tuple[str, ...] = (
    "auto_reframe_vertical",
    "beat_flash_montage",
    "captioned_tutorial",
    "podcast_clip",
    "product_showcase",
    "speed_ramp_drop",
    "storytelling_bg_music",
    "talking_head_meme",
)


def available_templates() -> tuple[str, ...]:
    return TEMPLATES


def run_init(
    template_name: str | None,
    dest_dir: Path | None,
    *,
    force: bool = False,
    list_only: bool = False,
) -> int:
    if list_only:
        print("Available templates:")
        for name in TEMPLATES:
            print(f"  {name}")
        return 0

    if template_name is None or dest_dir is None:
        print(
            "error: `template` and `dest_dir` are required unless --list is given",
            file=sys.stderr,
        )
        return 1

    if template_name not in TEMPLATES:
        print(
            f"error: unknown template '{template_name}'. "
            f"Run `eks-harness video init --list` to see available templates.",
            file=sys.stderr,
        )
        return 1

    cookbook_dir = _resolve_cookbook_dir()
    if cookbook_dir is None:
        print(
            f"error: the cookbook directory {COOKBOOK_DIR} is missing from this install.",
            file=sys.stderr,
        )
        return 1

    source = cookbook_dir / f"{template_name}.py"
    if not source.exists():
        print(
            f"error: template file missing from cookbook: {source}",
            file=sys.stderr,
        )
        return 1

    dest_dir.mkdir(parents=True, exist_ok=True)
    project_path = dest_dir / "project.py"

    if project_path.exists() and not force:
        print(
            f"error: {project_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    shutil.copyfile(source, project_path)

    assets_dir = dest_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    gitkeep = assets_dir / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")

    print(str(project_path))
    return 0


def _resolve_cookbook_dir() -> Path | None:
    return COOKBOOK_DIR if COOKBOOK_DIR.is_dir() else None
