"""``validate_project`` tool.

Loads a project (``project.py`` or ``project.json``) and reports any
pydantic validation errors. Designed to be the first thing an LLM client
calls after generating or editing a project - surfaces every error path so
the client can target a follow-up edit without guessing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..roots import RootsManager
from ._project_io import load_project_any, resolve_project_path

__all__ = ["DESCRIPTION", "run"]

_LOG = logging.getLogger(__name__)


def _run(project_path: Path, *, roots: RootsManager) -> dict[str, Any]:
    resolved = resolve_project_path(project_path, roots)
    try:
        loaded = load_project_any(resolved)
    except ValidationError as exc:
        return {
            "ok": False,
            "source_path": str(resolved),
            "source_kind": None,
            "errors": _format_validation_errors(exc),
        }
    except FileNotFoundError as exc:
        return {"ok": False, "source_path": str(resolved), "source_kind": None, "errors": [{"loc": "", "msg": str(exc), "type": "file_not_found"}]}
    except Exception as exc:
        return {
            "ok": False,
            "source_path": str(resolved),
            "source_kind": None,
            "errors": [{"loc": "", "msg": str(exc), "type": exc.__class__.__name__}],
        }

    return {
        "ok": True,
        "source_path": str(loaded.source_path),
        "source_kind": loaded.source_kind,
        "errors": [],
    }


def _format_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {
            "loc": ".".join(str(part) for part in err.get("loc", ())),
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]

DESCRIPTION = "Load a video project (project.py or project.json) and run pydantic validation. Returns {ok, errors} with each error's field path and message. Does not render."
run = _run
