"""M3 dev-server handlers: ``artifacts.list`` + ``artifacts.metadata``.

Reads the on-disk metadata.json + thumbnail.jpg pairs written by
:mod:`eks_harness.studio.artifacts` during render finalization. No registry
mutation, no subprocess work - pure on-disk lookups.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ... import artifacts as artifacts_mod
from ...roots import resolve_project_dir
from ..ws_hub import WSHub
from ..ws_protocol import (
    ArtifactsListRequest,
    ArtifactsMetadataRequest,
    ErrorEnvelope,
    ReplyEnvelope,
)

__all__ = [
    "handle_artifacts_list",
    "handle_artifacts_metadata",
    "register",
]

_LOG = logging.getLogger(__name__)


def _resolve_project_dir(project_id: str) -> Path:
    """Resolve a project id (or absolute path) to a directory.

    Delegates to the shared :func:`eks_harness.studio.roots.resolve_project_dir` so
    artifact listing/metadata resolve the same way as the file routes and the
    render path - including the "dev server launched inside the project dir"
    layout where the workspace itself is the project.
    """

    return resolve_project_dir(project_id)


async def handle_artifacts_list(
    request: ArtifactsListRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    project_dir = _resolve_project_dir(request.payload.project_id)
    try:
        artifacts = artifacts_mod.list_artifacts(project_dir)
    except Exception as exc:  # pragma: no cover - defensive
        return ErrorEnvelope(
            id=request.id, code="server_error", message=str(exc)
        )
    return ReplyEnvelope(
        type="artifacts.list.reply",
        id=request.id,
        result={
            "project_id": request.payload.project_id,
            "artifacts": [
                a.model_dump(mode="json", exclude_none=False) for a in artifacts
            ],
        },
    )


async def handle_artifacts_metadata(
    request: ArtifactsMetadataRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    project_dir = _resolve_project_dir(request.payload.project_id)
    body = artifacts_mod.read_metadata(
        project_dir=project_dir, job_id=request.payload.job_id
    )
    if body is None:
        return ErrorEnvelope(
            id=request.id,
            code="not_found",
            message=(
                f"no metadata for job {request.payload.job_id} in {project_dir}"
            ),
        )
    return ReplyEnvelope(
        type="artifacts.metadata.reply",
        id=request.id,
        result={
            "project_id": request.payload.project_id,
            "job_id": request.payload.job_id,
            "record": body.get("record"),
            "events": body.get("events", []),
            "schema_version": body.get("schema_version"),
        },
    )


def register(register_handler) -> None:
    """Wire the M3 artifact handlers into the endpoint dispatch registry."""

    register_handler("artifacts.list", handle_artifacts_list)
    register_handler("artifacts.metadata", handle_artifacts_metadata)
