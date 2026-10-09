"""Shared reference types returned by tools and exposed in resources.

These intentionally model the URI-based handles documented in the plan: a
``ProjectRef`` for a project workspace, a ``JobRef`` for a render job, a
``PluginRef`` for a project-local plugin. Callers receive small, opaque
references they can hand back to other tools or pass to ``ctx.read_resource``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EncodingPreset",
    "NullReporter",
    "Reporter",
    "JobRef",
    "JobStatus",
    "PluginRef",
    "ProjectRef",
    "RenderMode",
    "TimeRange",
]


RenderMode = Literal["preview", "final"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class ProjectRef(BaseModel):
    """Handle to a video project workspace on disk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(description="eks-harness://video/projects/<id> resource URI")
    project_id: str
    path: Path = Field(description="absolute path to the project workspace")


class JobRef(BaseModel):
    """Handle to a running or completed render job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(description="job://<id> URI")
    job_id: str
    project_uri: str
    status_uri: str = Field(description="eks-harness://video/render_jobs/<id> for live status")
    output_uri: str | None = Field(
        default=None,
        description="file:// URI for the rendered deliverable; populated once the job succeeds",
    )
    task_id: str | None = Field(
        default=None,
        description="MCP task identifier when the render was started as a task-augmented call",
    )


class PluginRef(BaseModel):
    """Handle to a project-local effect plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str
    name: str
    path: Path


class EncodingPreset(BaseModel):
    """Caller override for renderer encoding settings."""

    model_config = ConfigDict(extra="forbid")

    name: str
    encoder: str | None = None
    bitrate_kbps: int | None = None
    crf: int | None = None
    pix_fmt: str | None = None


class TimeRange(BaseModel):
    """Inclusive time slice in seconds, used to render a sub-range."""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(gt=0)


class ReporterSession(Protocol):
    async def send_resource_updated(self, **kwargs: Any) -> None: ...


class Reporter(Protocol):
    """What a long-running tool reports progress to (an MCP request context satisfies it)."""

    session: ReporterSession

    async def info(self, message: str) -> None: ...

    async def warning(self, message: str) -> None: ...

    async def report_progress(self, progress: float, total: float | None = None,
                              message: str | None = None) -> None: ...


class _NullSession:
    async def send_resource_updated(self, **kwargs: Any) -> None:
        return None


class NullReporter:
    """Reporter that forwards messages to the log and drops progress (used when nobody is listening)."""

    def __init__(self, logger_name: str = "eks_harness.studio") -> None:
        import logging

        self.session = _NullSession()
        self._log = logging.getLogger(logger_name)

    async def info(self, message: str) -> None:
        self._log.info("%s", message)

    async def warning(self, message: str) -> None:
        self._log.warning("%s", message)

    async def report_progress(self, progress: float, total: float | None = None,
                              message: str | None = None) -> None:
        return None
