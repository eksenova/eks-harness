"""Load a ``project.py`` file and return its top-level ``project``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

__all__ = ["load_project_file"]


def load_project_file(path: Path, *, module_name: str = "__eks_video_project__") -> Any:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"project file not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load project from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    project = getattr(module, "project", None)
    if project is None:
        raise RuntimeError(f"{path} does not define a top-level `project` variable")
    return project
