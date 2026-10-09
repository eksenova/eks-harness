"""Shared helpers for the IR-introspection / mutation tools.

The validate / inspect / extract_markers / add_segment / add_effect /
set_property tools all need to resolve a workspace-relative ``project_path``,
load a project (from ``project.py``, ``project.json``, or a directory holding
either), and serialize edits back to disk. Concentrating those concerns here
keeps the tool modules thin and consistent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eks_harness.video.ir import Project

from ..roots import RootsManager

__all__ = [
    "LoadedProject",
    "load_project_any",
    "load_project_json",
    "resolve_project_path",
    "write_project_json",
]


@dataclass(frozen=True)
class LoadedProject:
    """A loaded project plus the on-disk file it came from."""

    project: Project
    source_path: Path
    source_kind: str  # "py" or "json"


def resolve_project_path(project_path: Path | str, roots: RootsManager) -> Path:
    """Resolve a user-supplied ``project_path`` against the workspace root.

    Accepts an absolute path (used as-is) or a workspace-relative path
    (joined under :meth:`RootsManager.project_workspace`). Does not
    require the path to exist - callers decide whether existence is fatal.
    """

    raw = Path(project_path)
    if raw.is_absolute():
        return raw.resolve()
    workspace = roots.project_workspace()
    return (workspace / raw).resolve()


def _candidate_files(path: Path) -> tuple[Path, ...]:
    """Return the (project.py, project.json) candidates for ``path``.

    ``path`` may name a directory, ``project.py``, or ``project.json``.
    """

    if path.is_dir():
        return (path / "project.py", path / "project.json")
    return (path,)


def load_project_any(path: Path) -> LoadedProject:
    """Load a project from ``.py`` or ``.json``.

    When ``path`` is a directory, ``project.py`` is preferred over
    ``project.json`` because it is the editable source of truth.
    """

    for candidate in _candidate_files(path):
        if not candidate.exists():
            continue
        if candidate.suffix == ".py":
            return LoadedProject(
                project=_load_py(candidate),
                source_path=candidate,
                source_kind="py",
            )
        if candidate.suffix == ".json":
            return LoadedProject(
                project=_load_json(candidate),
                source_path=candidate,
                source_kind="json",
            )
        raise ValueError(f"unsupported project file extension: {candidate}")
    raise FileNotFoundError(
        f"no project.py or project.json found at {path}"
        if path.is_dir()
        else f"project file not found: {path}"
    )


def load_project_json(path: Path) -> tuple[Path, dict[str, Any]]:
    """Load the raw ``project.json`` dict for mutation tools.

    Returns the file path actually read and the parsed dict. ``path`` may be
    the JSON file or a directory containing it. Mutation tools refuse to
    operate on ``.py`` because Python source is not round-trippable in a
    safe, lossless way.
    """

    if path.is_dir():
        json_path = path / "project.json"
    elif path.suffix == ".json":
        json_path = path
    elif path.suffix == ".py":
        raise ValueError(
            f"this tool only operates on project.json (got {path}); "
            "re-author project.py manually or run `eks-harness video sync` first"
        )
    else:
        json_path = path
    if not json_path.exists():
        raise FileNotFoundError(f"project.json not found: {json_path}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{json_path} did not contain a JSON object")
    return json_path, data


def write_project_json(path: Path, project: Project) -> None:
    """Serialize ``project`` to ``path`` using the canonical formatting."""

    path.write_text(
        project.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )


def _load_py(path: Path) -> Project:
    spec = importlib.util.spec_from_file_location("__eks_studio_project__", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load project module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    project = getattr(module, "project", None)
    if project is None:
        raise RuntimeError(f"{path} does not define a top-level `project` variable")
    if not isinstance(project, Project):
        raise RuntimeError(
            f"{path}: `project` must be an eks_harness.video.Project instance, "
            f"got {type(project).__name__}"
        )
    return project


def _load_json(path: Path) -> Project:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Project.model_validate(raw, by_alias=True)
