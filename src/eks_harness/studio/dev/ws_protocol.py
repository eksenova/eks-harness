"""WebSocket wire-protocol pydantic models.

Single source of truth for the dev server's ``/ws`` dialect. Every client
request and every server event has a model here so handlers can rely on
typed payloads and the UI can mirror the shapes without drift.

Frames are JSON. Client requests carry ``id`` (correlation token); server
replies use the same ``id``. Server-pushed events omit ``id``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    # process logs (right sidebar LOGS tab)
    "ProcessLogEntry",
    "ProcessLogsPayload",
    "ProcessLogsRequest",
    "ProcessLogEventPayload",
    "ProcessLogEvent",
    "TOPIC_PROCESS_LOGS",
    # Shared payload models
    "Artifact",
    "CatalogEntry",
    "JobRecordSnapshot",
    "LogEntry",
    "MediaItem",
    "ProjectTimeline",
    "SystemSnapshot",
    "ReplyEnvelope",
    "ErrorEnvelope",
    # Client request models
    "SubscribeRequest",
    "UnsubscribeRequest",
    "ProjectSelectRequest",
    "ProjectRefreshRequest",
    "ProjectsListRequest",
    "RenderStartRequest",
    "RenderCancelRequest",
    "CatalogRequest",
    "PreviewRequestEffect",
    "PreviewRequestTransition",
    "PreviewCancelRequest",
    "MediaListRequest",
    "ArtifactsListRequest",
    "ArtifactsMetadataRequest",
    "ProjectTimelineRequest",
    "PingRequest",
    # media mutate (Agent 2)
    "MediaUploadRequest",
    "MediaDeleteRequest",
    "MediaRenameRequest",
    "ClientMessage",
    "ClientMessageAdapter",
    # Server event models
    "HelloEvent",
    "SystemSnapshotEvent",
    "SystemErrorEvent",
    "ProjectListEvent",
    "ProjectStateEvent",
    "ProjectChangedEvent",
    "CatalogEffectsEvent",
    "CatalogTransitionsEvent",
    "CatalogPreviewReadyEvent",
    "CatalogPreviewProgressEvent",
    "MediaListEvent",
    "JobCreatedEvent",
    "JobProgressEvent",
    "JobEvent",
    "JobPreviewFrameEvent",
    "JobSucceededEvent",
    "JobFailedEvent",
    "JobCancelledEvent",
    "LogEvent",
    "PongEvent",
    # media mutate (Agent 2)
    "MediaChangedEvent",
    "ServerMessage",
    "ServerMessageAdapter",
]


# ---------------------------------------------------------------------------
# Shared payload models
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    """Pydantic base with permissive extra handling for forward-compat."""

    model_config = ConfigDict(extra="allow")


class JobRecordSnapshot(_Base):
    """Snapshot of a render job, mirrored from ``jobs.JobRecord``."""

    job_id: str
    project_id: str
    project_path: str
    mode: str
    status: str
    progress: float = 0.0
    message: str = ""
    output_path: str | None = None
    error: str | None = None
    started_at: float
    updated_at: float
    current_step: str | None = None
    # 0-based index of the active orchestrator step (see
    # ``eks_harness.video.render.progress.STEP_ORDER``). ``None`` before the first
    # ``step_start`` arrives. UI consumers display ``step_index + 1``.
    step_index: int | None = None
    total_steps: int = 0
    frame_index: int = 0
    frame_total: int = 0
    eta_s: float | None = None
    error_category: str | None = None


class Artifact(_Base):
    """Persistent render artifact discovered under ``<project>/renders/``."""

    job_id: str
    mode: str
    status: str
    started_at: float
    finished_at: float | None = None
    duration_s: float | None = None
    output_url: str | None = None
    thumbnail_url: str | None = None
    total_frames: int | None = None
    error_category: str | None = None
    # Backend slice (item 4): scraped from the render event tail when a job
    # fails. ``category`` mirrors ``error_category`` for convenience; the
    # extra ``message`` / ``traceback`` / ``last_log`` fields surface the
    # actual reason so the UI can show something useful inline.
    failure_summary: dict[str, Any] | None = None


class CatalogEntry(_Base):
    """One effect or transition presented in the catalog grid.

    ``schema_`` carries the JSON schema (trailing underscore to avoid
    shadowing pydantic's ``BaseModel.schema`` method); it is serialised on
    the wire as ``schema`` via the alias.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    kind: Literal["effect", "transition"]
    description: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    default_params: dict[str, Any] | None = None
    preview_url: str | None = None
    key: str | None = None
    stale: bool = True
    # Populated only when ``previewable`` is False on the dict side; carries
    # a short human-readable reason so the UI can tooltip the dimmed tile.
    previewable_reason: str | None = None


class MediaItem(_Base):
    """Entry in the media library (project-local or global)."""

    path: str
    name: str
    url: str | None = None
    kind: Literal["video", "audio", "image"]
    duration: float | None = None
    dims: tuple[int, int] | None = None
    fps: float | None = None
    codec: str | None = None
    size_bytes: int | None = None


class SystemSnapshot(_Base):
    """Live system statistics. Missing/unavailable fields are omitted."""

    ts: float
    cpu_pct: float
    mem_pct: float
    gpu_pct: float | None = None
    cpu_temp_c: float | None = None
    gpu_temp_c: float | None = None
    render_pct: float | None = None


class LogEntry(_Base):
    """One log line broadcast through the ``log`` topic."""

    ts: float
    level: str
    source: str
    message: str
    payload: dict[str, Any] | None = None


class ProjectTimeline(_Base):
    """Flattened project timeline shape consumed by the UI timeline widget."""

    fps: float
    resolution: tuple[int, int]
    duration: float
    tracks: list[dict[str, Any]]
    audio_tracks: list[dict[str, Any]]
    markers: dict[str, Any]
    captions: dict[str, Any] | None = None
    transitions: list[dict[str, Any]]
    used: dict[str, bool]


class ReplyEnvelope(_Base):
    """Uniform envelope returned for any client request.

    The ``type`` is always ``<request_type>.reply``. The ``id`` echoes the
    request's correlation id. ``result`` carries the handler's response
    payload (free-form per request type).
    """

    type: str
    id: str
    result: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(_Base):
    """Uniform error envelope used in place of a reply when a handler fails."""

    type: Literal["error"] = "error"
    id: str | None = None
    code: str
    message: str
    detail: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Client → server request models
# ---------------------------------------------------------------------------


class _ClientBase(_Base):
    """Common fields for every client request."""

    id: str


class SubscribePayload(_Base):
    topics: list[str]


class SubscribeRequest(_ClientBase):
    type: Literal["subscribe"]
    payload: SubscribePayload


class UnsubscribeRequest(_ClientBase):
    type: Literal["unsubscribe"]
    payload: SubscribePayload


class ProjectSelectPayload(_Base):
    project_id: str


class ProjectSelectRequest(_ClientBase):
    type: Literal["project.select"]
    payload: ProjectSelectPayload


class ProjectRefreshRequest(_ClientBase):
    type: Literal["project.refresh"]
    payload: ProjectSelectPayload


class ProjectsListPayload(_Base):
    # Optional query - when present, the handler filters the response by
    # substring match on the project's name / path. Empty string disables
    # filtering. Keeping it on the request lets the UI debounce typing
    # without re-pulling the entire candidate list each keystroke.
    query: str = ""


class ProjectsListRequest(_ClientBase):
    type: Literal["projects.list"]
    payload: ProjectsListPayload = ProjectsListPayload()


class RenderStartPayload(_Base):
    project_id: str
    mode: Literal["preview", "final"] = "preview"
    preset: str | None = None
    encoder: str | None = None
    bitrate_kbps: int | None = None
    crf: int | None = None
    # Custom output resolution. Both axes must be supplied together to take
    # effect (a lone width or height is ambiguous and ignored).
    width: int | None = None
    height: int | None = None
    range_start: float | None = None
    range_end: float | None = None


class RenderStartRequest(_ClientBase):
    type: Literal["render.start"]
    payload: RenderStartPayload


class RenderCancelPayload(_Base):
    job_id: str


class RenderCancelRequest(_ClientBase):
    type: Literal["render.cancel"]
    payload: RenderCancelPayload


class CatalogRequestPayload(_Base):
    kind: Literal["effect", "transition"]


class CatalogRequest(_ClientBase):
    type: Literal["catalog.request"]
    payload: CatalogRequestPayload


class PreviewRequestPayload(_Base):
    name: str
    params: dict[str, Any] | None = None


class PreviewRequestEffect(_ClientBase):
    type: Literal["preview.request_effect"]
    payload: PreviewRequestPayload


class PreviewRequestTransition(_ClientBase):
    type: Literal["preview.request_transition"]
    payload: PreviewRequestPayload


class PreviewCancelPayload(_Base):
    """Identifies an in-flight preview request the client no longer wants.

    A request can be cancelled while it is still queued; a request that is
    already actively rendering is allowed to complete (killing ffmpeg
    mid-render only wastes the cycles we already spent). Either way the
    cancel handler removes the request from the queue and replies
    immediately - the UI should tolerate a trailing ``preview_ready`` /
    ``preview_progress`` event for the cancelled ``request_id``.
    """

    kind: Literal["effect", "transition"]
    name: str
    request_id: str | None = None


class PreviewCancelRequest(_ClientBase):
    type: Literal["preview.cancel"]
    payload: PreviewCancelPayload


class MediaListPayload(_Base):
    scope: Literal["project", "global"] = "project"
    kind: Literal["video", "audio", "image"] | None = None
    project_id: str | None = None


class MediaListRequest(_ClientBase):
    type: Literal["media.list"]
    payload: MediaListPayload


class ArtifactsListPayload(_Base):
    project_id: str


class ArtifactsListRequest(_ClientBase):
    type: Literal["artifacts.list"]
    payload: ArtifactsListPayload


class ArtifactsMetadataPayload(_Base):
    project_id: str
    job_id: str


class ArtifactsMetadataRequest(_ClientBase):
    type: Literal["artifacts.metadata"]
    payload: ArtifactsMetadataPayload


class ProjectTimelinePayload(_Base):
    project_id: str


class ProjectTimelineRequest(_ClientBase):
    type: Literal["project.timeline"]
    payload: ProjectTimelinePayload


class PingPayload(_Base):
    ts: float | None = None


class PingRequest(_ClientBase):
    type: Literal["ping"]
    payload: PingPayload = Field(default_factory=PingPayload)


# ---------------------------------------------------------------------------
# media mutate (Agent 2)
# ---------------------------------------------------------------------------


class MediaUploadPayload(_Base):
    project_id: str
    kind: Literal["video", "audio", "image"]
    filename: str
    data_base64: str
    overwrite: bool = False


class MediaUploadRequest(_ClientBase):
    type: Literal["media.upload"]
    payload: MediaUploadPayload


class MediaDeletePayload(_Base):
    project_id: str
    path: str


class MediaDeleteRequest(_ClientBase):
    type: Literal["media.delete"]
    payload: MediaDeletePayload


class MediaRenamePayload(_Base):
    project_id: str
    path: str
    new_name: str


class MediaRenameRequest(_ClientBase):
    type: Literal["media.rename"]
    payload: MediaRenamePayload


# ---------------------------------------------------------------------------
# Process logs - right sidebar LOGS tab streams the daemon process log
# (uvicorn lines, _LOG.info() output, etc) over WS. Append-only block below;
# any future addition should slot inside this banner so the diff stays clean.
# ---------------------------------------------------------------------------


TOPIC_PROCESS_LOGS = "process_logs"
"""WSHub topic that broadcasts ``ProcessLogEvent`` frames."""


class ProcessLogEntry(_Base):
    """One captured log record from the daemon process itself."""

    ts: float
    level: str
    source: str
    message: str


class ProcessLogsPayload(_Base):
    """Request payload for the initial backlog fetch."""

    # Only entries with ``ts > since_ts`` are returned. Omitted = full backlog.
    since_ts: float | None = None
    # Soft cap on returned entries (clamped server-side). Default keeps the
    # initial response under ~256 KB even with verbose lines.
    limit: int = 512


class ProcessLogsRequest(_ClientBase):
    type: Literal["process.logs"]
    payload: ProcessLogsPayload = Field(default_factory=ProcessLogsPayload)


class ProcessLogEventPayload(_Base):
    """Batched live-tail payload broadcast on ``TOPIC_PROCESS_LOGS``."""

    entries: list[ProcessLogEntry]


class ProcessLogEvent(_Base):
    type: Literal["process.log"] = "process.log"
    payload: ProcessLogEventPayload


ClientMessage = Annotated[
    Union[
        SubscribeRequest,
        UnsubscribeRequest,
        ProjectSelectRequest,
        ProjectRefreshRequest,
        ProjectsListRequest,
        RenderStartRequest,
        RenderCancelRequest,
        CatalogRequest,
        PreviewRequestEffect,
        PreviewRequestTransition,
        PreviewCancelRequest,
        MediaListRequest,
        ArtifactsListRequest,
        ArtifactsMetadataRequest,
        ProjectTimelineRequest,
        PingRequest,
        # media mutate (Agent 2)
        MediaUploadRequest,
        MediaDeleteRequest,
        MediaRenameRequest,
        # process logs (right-sidebar LOGS tab)
        ProcessLogsRequest,
    ],
    Field(discriminator="type"),
]

ClientMessageAdapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# ---------------------------------------------------------------------------
# Server → client event models
# ---------------------------------------------------------------------------


class HelloPayload(_Base):
    server: str
    version: str
    project_workspace: str | None = None
    media_library_root: str | None = None


class HelloEvent(_Base):
    type: Literal["hello"] = "hello"
    payload: HelloPayload


class SystemSnapshotEvent(_Base):
    type: Literal["system.snapshot"] = "system.snapshot"
    payload: SystemSnapshot


class SystemErrorPayload(_Base):
    code: str
    message: str
    detail: dict[str, Any] | None = None


class SystemErrorEvent(_Base):
    type: Literal["system.error"] = "system.error"
    payload: SystemErrorPayload


class ProjectListItem(_Base):
    id: str
    name: str
    path: str


class ProjectListPayload(_Base):
    projects: list[ProjectListItem]


class ProjectListEvent(_Base):
    type: Literal["project.list"] = "project.list"
    payload: ProjectListPayload


class ProjectStatePayload(_Base):
    project_id: str
    project: dict[str, Any]


class ProjectStateEvent(_Base):
    type: Literal["project.state"] = "project.state"
    payload: ProjectStatePayload


class ProjectChangedPayload(_Base):
    project_id: str
    rev: int


class ProjectChangedEvent(_Base):
    type: Literal["project.changed"] = "project.changed"
    payload: ProjectChangedPayload


class CatalogEntriesPayload(_Base):
    entries: list[CatalogEntry]


class CatalogEffectsEvent(_Base):
    type: Literal["catalog.effects"] = "catalog.effects"
    payload: CatalogEntriesPayload


class CatalogTransitionsEvent(_Base):
    type: Literal["catalog.transitions"] = "catalog.transitions"
    payload: CatalogEntriesPayload


class CatalogPreviewReadyPayload(_Base):
    kind: Literal["effect", "transition"]
    name: str
    url: str
    key: str


class CatalogPreviewReadyEvent(_Base):
    type: Literal["catalog.preview_ready"] = "catalog.preview_ready"
    payload: CatalogPreviewReadyPayload


class CatalogPreviewProgressPayload(_Base):
    """Step-by-step status for a single preview render request.

    Phases progress as ``queued`` -> ``rendering`` -> ``encoding`` ->
    ``done`` for a successful render, or terminate in ``failed`` on error.
    ``queue_position`` (1-based) is only meaningful for ``queued``;
    ``progress`` is a 0..1 hint when known. ``catalog.preview_ready``
    remains the canonical "the PNG is on disk" signal - ``done`` is fired
    immediately afterwards so the UI can clear its overlay.
    """

    kind: Literal["effect", "transition"]
    name: str
    request_id: str
    phase: Literal["queued", "rendering", "encoding", "done", "failed"]
    queue_position: int | None = None
    progress: float | None = None
    message: str | None = None


class CatalogPreviewProgressEvent(_Base):
    type: Literal["catalog.preview_progress"] = "catalog.preview_progress"
    payload: CatalogPreviewProgressPayload


class MediaListEventPayload(_Base):
    scope: Literal["project", "global"]
    kind: Literal["video", "audio", "image"] | None = None
    items: list[MediaItem]


class MediaListEvent(_Base):
    type: Literal["media.list"] = "media.list"
    payload: MediaListEventPayload


class JobCreatedEvent(_Base):
    type: Literal["job.created"] = "job.created"
    payload: JobRecordSnapshot


class JobProgressEvent(_Base):
    type: Literal["job.progress"] = "job.progress"
    payload: JobRecordSnapshot


class JobEventPayload(_Base):
    job_id: str
    event: Literal["step_start", "step_end", "substep_start", "substep_end", "log", "error"]
    data: dict[str, Any] = Field(default_factory=dict)


class JobEvent(_Base):
    type: Literal["job.event"] = "job.event"
    payload: JobEventPayload


class JobPreviewFramePayload(_Base):
    job_id: str
    frame: int
    url: str
    t_s: float | None = None


class JobPreviewFrameEvent(_Base):
    type: Literal["job.preview_frame"] = "job.preview_frame"
    payload: JobPreviewFramePayload


class JobSucceededPayload(JobRecordSnapshot):
    artifact: Artifact | None = None


class JobSucceededEvent(_Base):
    type: Literal["job.succeeded"] = "job.succeeded"
    payload: JobSucceededPayload


class JobFailedPayload(JobRecordSnapshot):
    # Backend slice: scraped at finalize time from the last error / log
    # events so the UI can show a concrete reason without round-tripping
    # through artifacts.metadata.
    failure_summary: dict[str, Any] | None = None


class JobFailedEvent(_Base):
    type: Literal["job.failed"] = "job.failed"
    payload: JobFailedPayload


class JobCancelledEvent(_Base):
    type: Literal["job.cancelled"] = "job.cancelled"
    payload: JobRecordSnapshot


class LogEvent(_Base):
    type: Literal["log"] = "log"
    payload: LogEntry


class ErrorEvent(_Base):
    type: Literal["error"] = "error"
    id: str | None = None
    code: str
    message: str
    detail: dict[str, Any] | None = None


class PongPayload(_Base):
    ts: float


class PongEvent(_Base):
    type: Literal["pong"] = "pong"
    payload: PongPayload


# ---------------------------------------------------------------------------
# media mutate broadcast events (Agent 2)
# ---------------------------------------------------------------------------


class MediaChangedPayload(_Base):
    project_id: str


class MediaChangedEvent(_Base):
    type: Literal["media.changed"] = "media.changed"
    payload: MediaChangedPayload


ServerMessage = Annotated[
    Union[
        HelloEvent,
        SystemSnapshotEvent,
        SystemErrorEvent,
        ProjectListEvent,
        ProjectStateEvent,
        ProjectChangedEvent,
        CatalogEffectsEvent,
        CatalogTransitionsEvent,
        CatalogPreviewReadyEvent,
        CatalogPreviewProgressEvent,
        MediaListEvent,
        JobCreatedEvent,
        JobProgressEvent,
        JobEvent,
        JobPreviewFrameEvent,
        JobSucceededEvent,
        JobFailedEvent,
        JobCancelledEvent,
        LogEvent,
        ErrorEvent,
        PongEvent,
        # media mutate (Agent 2)
        MediaChangedEvent,
        # process logs (right-sidebar LOGS tab)
        ProcessLogEvent,
    ],
    Field(discriminator="type"),
]

ServerMessageAdapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
