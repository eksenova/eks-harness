from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

T = TypeVar("T")

LeaseKind = Literal["browser", "ios", "android"]
DeviceKind = Literal["ios", "android"]
LeaseState = Literal["queued", "active", "idle", "released", "broken"]
LeasePhase = Literal["preparing", "ready", "failed"]
OwnerKind = Literal["agent", "manual"]
Role = Literal["admin", "member"]
GrantLevel = Literal["viewer", "editor"]
AccessLevel = Literal["viewer", "editor", "admin"]
ArtifactSource = Literal["agent", "cli", "ui", "mcp"]
PoolAction = Literal["start", "shutdown", "reset", "delete"]
AuthVia = Literal["local", "key", "cookie"]
AcquireStatus = Literal["granted", "preparing", "queued", "failed"]
BackendStatus = Literal["stopped", "preparing", "starting", "running", "stopping", "failed"]
SettingSource = Literal["default", "file", "env"]
TimelineEntryType = Literal["event", "note", "artifact"]
ARTIFACT_KINDS = ("screenshot", "video", "dom", "mhtml", "a11y", "har", "console", "log", "site", "file")
SHARE_EXPIRY_PRESETS = ("1h", "1d", "7d", "30d")


def ts_to_datetime(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def datetime_to_ts(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True,
                              ser_json_timedelta="float")


class ErrorResponse(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")
    error: str
    message: str


class ValidationProblem(ApiModel):
    field: str
    message: str
    type: str = ""


class ValidationErrorResponse(ErrorResponse):
    problems: list[ValidationProblem] = Field(default_factory=list)


class LeaseReleasedError(ErrorResponse):
    project: str | None = None
    session: str | None = None
    reacquire: str
    sid: str
    state: str = "released"


class OkResponse(ApiModel):
    ok: bool = True


class Page(ApiModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    version: str


class VersionResponse(ApiModel):
    version: str
    source_hash: str | None = None
    built_at: str | None = None
    editable: bool = False
    python: str
    platform: str


class FileDescriptorUsage(ApiModel):
    open: int
    limit: int | None = None
    db_connections: int | None = None
    pressure: bool = False


class StorageUsage(ApiModel):
    used_bytes: int
    artifact_bytes: int
    artifact_count: int
    quota_bytes: int | None = None
    over_quota: bool = False
    free_disk_bytes: int | None = None


class DaemonActionResponse(ApiModel):
    ok: bool = True
    action: Literal["restart", "stop"]
    message: str = ""


class EventOut(ApiModel):
    id: int
    ts: datetime
    type: str
    resource: str | None = None
    lease_sid: str | None = None
    session_id: int | None = None
    project_id: str | None = None
    actor: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class EventList(ApiModel):
    items: list[EventOut]


class SettingOut(ApiModel):
    key: str
    value: Any = None
    default: Any = None
    description: str
    type: str
    restart_required: bool
    group: str
    group_title: str
    nullable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None
    source: SettingSource = "default"
    env_name: str
    pending_restart: bool = False


class SettingsResponse(ApiModel):
    settings: list[SettingOut]
    config_file: str
    restart_pending: bool = False
    restart_pending_keys: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class SettingsPatch(ApiModel):
    values: dict[str, Any] = Field(default_factory=dict)
    unset: list[str] = Field(default_factory=list)


class SettingChange(ApiModel):
    key: str
    old: Any = None
    new: Any = None
    restart_required: bool = False


class SettingsPatchResponse(ApiModel):
    changed: list[SettingChange]
    restart_required: bool = False
    settings: SettingsResponse | None = None


class SettingsAuditOut(ApiModel):
    id: int
    ts: datetime
    user: str | None = None
    key: str
    old: Any = None
    new: Any = None


class UserOut(ApiModel):
    id: int
    username: str
    role: Role
    disabled: bool = False
    builtin: bool = False
    has_password: bool = False
    created_at: datetime
    updated_at: datetime | None = None


class UserList(ApiModel):
    items: list[UserOut]


class UserCreate(ApiModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
    password: str | None = Field(default=None, min_length=8, max_length=512)
    role: Role = "member"


class UserUpdate(ApiModel):
    username: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
    password: str | None = Field(default=None, min_length=8, max_length=512)
    role: Role | None = None
    disabled: bool | None = None


class GrantOut(ApiModel):
    id: int
    user_id: int
    username: str | None = None
    project_id: str
    session_id: int | None = None
    session_slug: str | None = None
    session_name: str | None = None
    level: GrantLevel
    created_at: datetime


class GrantList(ApiModel):
    items: list[GrantOut]


class GrantCreate(ApiModel):
    user_id: int | None = None
    username: str | None = None
    project: str
    session: str | None = None
    level: GrantLevel = "viewer"


class LoginRequest(ApiModel):
    username: str | None = None
    password: str | None = None
    api_key: str | None = None


class LoginResponse(ApiModel):
    user: UserOut
    csrf_token: str
    expires_at: datetime


class MeResponse(ApiModel):
    user: UserOut
    via: AuthVia
    auth_enabled: bool
    csrf_token: str | None = None
    key_prefix: str | None = None
    grants: list[GrantOut] = Field(default_factory=list)


class ApiKeyOut(ApiModel):
    id: int
    user_id: int
    username: str | None = None
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    active: bool = True


class ApiKeyList(ApiModel):
    items: list[ApiKeyOut]


class ApiKeyCreate(ApiModel):
    name: str = Field(default="", max_length=100)
    user_id: int | None = None
    username: str | None = None


class ApiKeyCreated(ApiKeyOut):
    key: str


class ProjectBrief(ApiModel):
    id: str
    owner: str
    name: str
    title: str = ""
    url: str


class SessionBrief(ApiModel):
    id: int
    project_id: str | None = None
    name: str
    slug: str
    url: str
    shared_url: str


class LeaseBrief(ApiModel):
    id: str
    sid: str
    kind: LeaseKind
    resource: str | None = None
    state: LeaseState
    phase: LeasePhase | None = None
    owner_instance: str
    owner_kind: OwnerKind = "agent"
    owner_user: int | None = None
    owner_username: str | None = None
    acquired_at: datetime | None = None
    heartbeat_at: datetime | None = None
    idle_seconds: float | None = None


class ProjectStatsOut(ApiModel):
    session_count: int = 0
    artifact_count: int = 0
    unseen_count: int = 0
    size_bytes: int = 0
    last_activity_at: datetime | None = None


class ProjectOut(ApiModel):
    id: str
    owner: str
    name: str
    title: str = ""
    description: str = ""
    implicit: bool = False
    retention_days: int | None = None
    default_retention_days: int | None = None
    effective_retention_days: int | None = None
    retention_source: Literal["project", "forever", "global"] = "global"
    created_at: datetime
    updated_at: datetime
    session_count: int = 0
    artifact_count: int = 0
    unseen_count: int = 0
    size_bytes: int = 0
    last_activity_at: datetime | None = None
    project_file_count: int = 0
    active_leases: int = 0
    access: AccessLevel | None = None
    url: str


class ProjectList(ApiModel):
    items: list[ProjectOut]


class ProjectCreate(ApiModel):
    id: str | None = None
    owner: str | None = None
    name: str | None = None
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=10000)
    retention_days: int | None = Field(default=None, ge=0, le=36500)

    def project_id(self) -> str:
        if self.id:
            return self.id
        if self.owner and self.name:
            return f"{self.owner}/{self.name}"
        raise ValueError("send id (owner/name) or owner and name")


class ProjectUpdate(ApiModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    retention_days: int | None = Field(default=None, ge=0, le=36500)


class DeleteSummary(ApiModel):
    deleted: bool = True
    artifacts: int = 0
    sessions: int = 0
    notes: int = 0
    shares: int = 0
    bytes: int = 0


class SessionProjectOut(ApiModel):
    project: ProjectBrief
    artifact_count: int = 0
    unseen_count: int = 0
    size_bytes: int = 0
    active_leases: int = 0
    last_active_at: datetime | None = None
    url: str


class SessionOut(ApiModel):
    id: int
    project_id: str | None = None
    project_ids: list[str] = Field(default_factory=list)
    projects: list[SessionProjectOut] = Field(default_factory=list)
    name: str
    slug: str
    created_at: datetime
    last_active_at: datetime
    artifact_count: int = 0
    unseen_count: int = 0
    size_bytes: int = 0
    note_count: int = 0
    active_leases: list[LeaseBrief] = Field(default_factory=list)
    access: AccessLevel | None = None
    url: str
    shared_url: str


class SessionList(ApiModel):
    items: list[SessionOut]


class SessionUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, min_length=1, max_length=100)


class NoteCreate(ApiModel):
    body: str = Field(min_length=1, max_length=20000)
    sid: str | None = None


class NoteOut(ApiModel):
    id: int
    session_id: int
    lease_sid: str | None = None
    author: str
    body: str
    created_at: datetime


class ArtifactLinks(ApiModel):
    id: str
    url: str
    raw_url: str
    session_url: str
    shared_session_url: str | None = None
    download_url: str
    kind: str
    size: int
    caption: str = ""


class ArtifactOut(ArtifactLinks):
    project_id: str
    session_id: int | None = None
    session_slug: str | None = None
    session_name: str | None = None
    lease_sid: str | None = None
    mime: str
    filename: str
    sha256: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    pinned: bool = False
    source: ArtifactSource = "cli"
    created_by: str | None = None
    created_at: datetime
    meta: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    retention_days: int | None = None
    effective_retention_days: int | None = None
    retention_source: Literal["pinned", "artifact", "project", "global"] | None = None
    expires_at: datetime | None = None
    seen: bool = False
    thumbnail_url: str | None = None
    site_url: str | None = None
    share_count: int = 0
    previous_id: str | None = None
    next_id: str | None = None


class ArtifactFacets(ApiModel):
    kind: dict[str, int] = Field(default_factory=dict)
    tag: dict[str, int] = Field(default_factory=dict)


class ArtifactList(ApiModel):
    items: list[ArtifactOut]
    next_cursor: str | None = None
    prev_cursor: str | None = None
    total: int | None = None
    facets: ArtifactFacets | None = None


class ArtifactUpdate(ApiModel):
    caption: str | None = Field(default=None, max_length=5000)
    pinned: bool | None = None
    tags: list[str] | None = None
    add_tags: list[str] | None = None
    remove_tags: list[str] | None = None
    meta: dict[str, Any] | None = None
    retention_days: int | None = Field(default=None, ge=1, le=36500)


class ArtifactBulkRetentionRequest(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    retention_days: int | None = Field(default=None, ge=1, le=36500)


class SeenBulkRequest(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    seen: bool = True


class SeenResponse(ApiModel):
    updated: int
    seen: bool


class ArtifactBulkTagRequest(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class ArtifactBulkDeleteRequest(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=5000)


class SearchHitOut(ApiModel):
    artifact: ArtifactOut
    snippet: str = ""
    rank: float = 0.0


class SearchResponse(ApiModel):
    query: str
    items: list[SearchHitOut]


class TagCount(ApiModel):
    tag: str
    count: int
    color: str | None = None
    builtin: bool = False
    label: str | None = None


class TagInfo(ApiModel):
    tag: str
    label: str
    color: str | None = None
    default_color: str | None = None
    builtin: bool = False
    description: str = ""
    count: int = 0


class TagCatalog(ApiModel):
    items: list[TagInfo]


class TagColorUpdate(ApiModel):
    color: str | None = Field(default=None, max_length=16)


class TimelineEntry(ApiModel):
    ts: datetime
    type: TimelineEntryType
    event: EventOut | None = None
    note: NoteOut | None = None
    artifact: ArtifactOut | None = None


class TimelineResponse(ApiModel):
    session: SessionOut
    items: list[TimelineEntry]


class ShareCreate(ApiModel):
    expires: str | None = None
    expires_at: datetime | None = None

    @field_validator("expires")
    @classmethod
    def _expires(cls, value: str | None) -> str | None:
        if value in (None, "", "never", "none"):
            return None
        text = value.strip().lower()
        if text[:-1].isdigit() and text[-1] in "mhdw" and int(text[:-1]) > 0:
            return text
        raise ValueError("expires is 1h, 1d, 7d, 30d, another <n>m|h|d|w, or never")


class ShareOut(ApiModel):
    token: str
    artifact_id: str
    url: str
    raw_url: str
    direct_url: str | None = None
    created_by: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    views: int = 0
    last_viewed_at: datetime | None = None
    active: bool = True


class ShareList(ApiModel):
    items: list[ShareOut]


class SharedArtifact(ApiModel):
    kind: str
    mime: str
    filename: str
    size: int
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    caption: str = ""
    created_at: datetime
    url: str
    raw_url: str
    direct_url: str
    download_url: str
    site_url: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SharedArtifactOut(ApiModel):
    token: str
    artifact: SharedArtifact
    expires_at: datetime | None = None


class LeaseUrls(ApiModel):
    session: str
    ui: str
    project: str | None = None


class DeviceBrief(ApiModel):
    key: str
    kind: DeviceKind
    index: int
    name: str
    udid: str | None = None
    serial: str | None = None
    port: int | None = None
    status: str | None = None


class LeaseOut(LeaseBrief):
    session_id: int | None = None
    project_id: str | None = None
    session_slug: str | None = None
    session_name: str | None = None
    backend_id: str | None = None
    reason: str | None = None
    error: str | None = None
    queued_at: datetime | None = None
    released_at: datetime | None = None
    idle_limit: float | None = None
    label: str | None = None
    tree: str | None = None
    state_dir: str | None = None
    previous_sid: str | None = None
    stale: str | None = None
    queue_position: int | None = None
    cdp: str | None = None
    browser: int | None = None
    profile: int | None = None
    device: DeviceBrief | None = None
    urls: LeaseUrls | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class LeaseBrowserResponse(ApiModel):
    sid: str
    resource: str
    browser: int
    profile: int
    cdp: str
    restarted: bool = False


class LeaseList(ApiModel):
    items: list[LeaseOut]
    queue: list[LeaseOut] = Field(default_factory=list)


class QueueHolder(ApiModel):
    sid: str
    instance: str
    resource: str | None = None
    idle_seconds: float | None = None
    session: str | None = None


class LeaseAcquireRequest(ApiModel):
    kind: LeaseKind
    project: str
    session: str
    instance: str
    backend: str | None = None
    wait: bool = True
    owner_pid: int | None = None
    owner_started: str | None = None
    tree: str | None = None
    state_dir: str | None = None
    label: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _alias_kind(cls, value: Any) -> Any:
        return "browser" if value == "chrome" else value


class LeaseAcquireResponse(ApiModel):
    status: AcquireStatus
    sid: str | None = None
    lease: LeaseOut | None = None
    project: ProjectBrief | None = None
    session: SessionBrief | None = None
    resource: str | None = None
    urls: LeaseUrls | None = None
    position: int | None = None
    waiting: int | None = None
    holders: list[QueueHolder] = Field(default_factory=list)
    error: str | None = None


class LeaseResumeRequest(ApiModel):
    sid: str
    instance: str | None = None
    wait: bool = True
    backend: str | None = None
    owner_pid: int | None = None
    owner_started: str | None = None
    tree: str | None = None
    state_dir: str | None = None
    label: str | None = None


class LeaseHeartbeatResponse(ApiModel):
    sid: str
    state: LeaseState
    phase: LeasePhase | None = None
    heartbeat_at: datetime | None = None


class LeaseIdleRequest(ApiModel):
    grace: float | None = Field(default=None, ge=0)


class LeaseIdleResponse(ApiModel):
    sid: str
    grace: float
    state: LeaseState


class LeaseReleaseRequest(ApiModel):
    reason: str | None = None


class LeaseBreakRequest(ApiModel):
    sid: str | None = None
    lease: str | None = None
    reason: str = "broken by hand"


class LeaseReleaseResponse(ApiModel):
    released: list[str] = Field(default_factory=list)
    sids: list[str] = Field(default_factory=list)


class InstanceEndedRequest(ApiModel):
    reason: str | None = None


class SidInfo(ApiModel):
    sid: str
    valid: bool
    lease: LeaseOut
    project: ProjectBrief | None = None
    session: SessionBrief | None = None
    urls: LeaseUrls | None = None
    reacquire: str | None = None


class ActivityOut(ApiModel):
    events: list[EventOut] = Field(default_factory=list)


class DeviceOut(ApiModel):
    key: str
    kind: DeviceKind
    index: int
    name: str
    status: str
    status_text: str
    status_since: datetime | None = None
    udid: str | None = None
    serial: str | None = None
    port: int | None = None
    retired: bool = False
    last_used_at: datetime | None = None
    idle_seconds: float | None = None
    lease: LeaseBrief | None = None
    session: SessionBrief | None = None
    queue_length: int = 0
    live_url: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    live_viewers: int = 0


class DeviceList(ApiModel):
    items: list[DeviceOut]
    max_running: int
    running: int


class DeviceDetail(DeviceOut):
    queue: list[LeaseOut] = Field(default_factory=list)
    activity: list[EventOut] = Field(default_factory=list)
    history: list[LeaseOut] = Field(default_factory=list)
    log_resource: str


class ProfileOut(ApiModel):
    id: str
    browser: int
    profile: int
    status: str
    status_text: str
    status_since: datetime | None = None
    page_url: str | None = None
    lease: LeaseBrief | None = None
    session: SessionBrief | None = None
    queue_length: int = 0
    live_url: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    live_viewers: int = 0


class ProfileList(ApiModel):
    items: list[ProfileOut]


class ProfileDetail(ProfileOut):
    queue: list[LeaseOut] = Field(default_factory=list)
    activity: list[EventOut] = Field(default_factory=list)
    history: list[LeaseOut] = Field(default_factory=list)
    cdp: str | None = None


class BrowserProcessOut(ApiModel):
    index: int
    status: str
    status_text: str
    alive: bool = False
    pid: int | None = None
    port: int | None = None
    cdp: str | None = None
    binary: str | None = None
    launched_at: datetime | None = None
    last_used_at: datetime | None = None
    profiles: list[ProfileOut] = Field(default_factory=list)
    log_resource: str


class BrowserList(ApiModel):
    items: list[BrowserProcessOut]
    capacity: int
    command: str


class BrowserDetail(BrowserProcessOut):
    activity: list[EventOut] = Field(default_factory=list)


class PoolActionRequest(ApiModel):
    confirm: str | None = None
    reason: str | None = None
    force: bool = False


class PoolActionResponse(ApiModel):
    resource: str
    action: PoolAction
    ok: bool = True
    broken_leases: list[str] = Field(default_factory=list)
    lease: LeaseOut | None = None
    message: str = ""


class BackendEnsureRequest(ApiModel):
    definition: str
    instance: str
    id: str | None = None
    tree: str | None = None
    hold: float = Field(default=300, ge=0)
    hold_id: str | None = None
    restart: bool = False


class BackendStopRequest(ApiModel):
    final: bool = False
    reason: str | None = None


class BackendHoldRequest(ApiModel):
    seconds: float = 300
    hold_id: str | None = None


class BackendOut(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")
    id: str
    definition: str
    instance: str
    status: str
    tree: str | None = None
    description: str | None = None
    ports: dict[str, int] = Field(default_factory=dict)
    captured: dict[str, str] = Field(default_factory=dict)
    exports: dict[str, str] = Field(default_factory=dict)
    processes: list[str] = Field(default_factory=list)
    logs: dict[str, str] = Field(default_factory=dict)
    alive: dict[str, bool] = Field(default_factory=dict)
    bindings: list[str] = Field(default_factory=list)
    error: str | None = None
    fingerprint: str | None = None
    idle_grace_seconds: float | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    log_file: str | None = None
    redacted: bool = False


class BackendList(ApiModel):
    items: list[BackendOut]


class BackendDefinitionOut(ApiModel):
    name: str
    path: str
    source: Literal["tree", "config", "plugin"]
    tree: str | None = None
    plugin: str | None = None
    description: str | None = None


class BackendDefinitionList(ApiModel):
    items: list[BackendDefinitionOut]


class LogResponse(ApiModel):
    resource: str
    path: str
    offset: int = 0
    text: str = ""


class LogResourceOut(ApiModel):
    resource: str
    label: str
    path: str


class LogResourceList(ApiModel):
    items: list[LogResourceOut]


class CaptureRequest(ApiModel):
    caption: str = ""
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    source: ArtifactSource | None = None
    trim: bool | None = None
    pace: Any | None = None
    from_here: bool | None = None
    no_pointer: bool | None = None
    hide: list[str] | None = None

    def wants_trim(self) -> bool:
        if self.trim is not None:
            return self.trim
        value = self.meta.get("trim")
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return True if value is None else bool(value)


class AnnotateRequest(ApiModel):
    spec: Any
    style: str | None = None
    dry_run: bool = False


class AnnotateRerenderRequest(ApiModel):
    style: str | None = None


class AnnotateRecaptureRequest(ApiModel):
    sid: str | None = None


class AnnotateRestoreRequest(ApiModel):
    version: int = Field(ge=1)


class AnnotationRuleResult(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")
    rule: str
    ok: bool
    message: str = ""


class AnnotationValidation(ApiModel):
    ok: bool
    rules: list[AnnotationRuleResult] = Field(default_factory=list)
    anchor_fallback: bool = False


class AnnotationCrop(ApiModel):
    item_id: str
    url: str
    raw_url: str


class AnnotateResponse(ApiModel):
    artifact: ArtifactOut
    version: int
    kind: str
    validation: AnnotationValidation
    crops: list[AnnotationCrop] = Field(default_factory=list)


class AnnotationVersionOut(ApiModel):
    version: int
    kind: str
    created_at: datetime
    created_by: str | None = None
    style_name: str | None = None
    url: str


class AnnotationVersionsResponse(ApiModel):
    versions: list[AnnotationVersionOut] = Field(default_factory=list)


class AnnotateRestoreResponse(ApiModel):
    artifact: ArtifactOut
    version: int


class ProjectAssetOut(ApiModel):
    name: str
    filename: str
    mime: str = "application/octet-stream"
    size: int = 0
    sha256: str = ""
    created_at: datetime
    url: str


class ProjectAssetList(ApiModel):
    items: list[ProjectAssetOut] = Field(default_factory=list)


class VideoStopResponse(ArtifactOut):
    full: ArtifactOut | None = None
    encoding: list[str] = Field(default_factory=list)


class VideoStartResponse(ApiModel):
    sid: str
    resource: str
    recording: bool = True
    started_at: datetime


class VideoResetResponse(ApiModel):
    sid: str
    resource: str
    device: str
    stopped_recorder: bool = False
    orphan: bool = False
    restarted: bool = False


class StatusResponse(ApiModel):
    pid: int
    managed: bool = False
    fake_pools: bool = False
    version: str
    source_hash: str | None = None
    url: str
    public_url: str
    started_at: datetime
    uptime_seconds: float
    auth_enabled: bool
    config_file: str
    restart_pending: bool = False
    leases: list[LeaseOut] = Field(default_factory=list)
    queue: list[LeaseOut] = Field(default_factory=list)
    devices: list[DeviceOut] = Field(default_factory=list)
    browsers: list[BrowserProcessOut] = Field(default_factory=list)
    backends: list[BackendOut] = Field(default_factory=list)
    storage: StorageUsage | None = None
    file_descriptors: FileDescriptorUsage | None = None
