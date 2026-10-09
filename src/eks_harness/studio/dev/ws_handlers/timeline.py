"""M6 dev-server handler: ``project.timeline``.

The handler resolves the requested project id to a directory (same tolerance
as M3's artifact handlers), then drives :func:`timeline.build_timeline_async`
to flatten the IR + marker cache into the UI-friendly JSON the dev UI's
timeline widget expects.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...roots import resolve_project_dir
from ..timeline import TimelineLoadError, build_timeline_async
from ..ws_hub import WSHub
from ..ws_protocol import (
    ErrorEnvelope,
    ProjectTimelineRequest,
    ReplyEnvelope,
)

__all__ = ["handle_project_timeline", "register"]

_LOG = logging.getLogger(__name__)


def _resolve_project_dir(project_id: str) -> Path:
    """Resolve a project id (or absolute path) to a directory.

    Delegates to the shared :func:`eks_harness.studio.roots.resolve_project_dir` so
    the timeline resolves the same way as artifacts / file routes / render -
    including the "dev server launched inside the project dir" layout.
    """

    return resolve_project_dir(project_id)


async def handle_project_timeline(
    request: ProjectTimelineRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    project_dir = _resolve_project_dir(request.payload.project_id)
    if not project_dir.exists():
        return ErrorEnvelope(
            id=request.id,
            code="not_found",
            message=f"no project directory at {project_dir}",
        )
    if not (project_dir / "project.py").exists():
        return ErrorEnvelope(
            id=request.id,
            code="not_found",
            message=f"no project.py at {project_dir}",
        )

    try:
        timeline = await build_timeline_async(project_dir)
    except TimelineLoadError as exc:
        return ErrorEnvelope(
            id=request.id,
            code="project_load_error",
            message=str(exc),
            detail={"stderr": exc.detail} if exc.detail else None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.exception("project.timeline failed for %s", project_dir)
        return ErrorEnvelope(
            id=request.id, code="server_error", message=str(exc)
        )

    return ReplyEnvelope(
        type="project.timeline.reply",
        id=request.id,
        result=timeline,
    )


def register(register_handler) -> None:
    """Wire the M6 timeline handler into the endpoint dispatch registry."""

    register_handler("project.timeline", handle_project_timeline)
