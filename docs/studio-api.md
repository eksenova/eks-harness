# Studio API

The studio is the video editing surface of eks-harness. Its server side lives
in `eks_harness.studio`; the daemon mounts `studio_router(ctx)` under
`/api/studio`. This document lists every HTTP route and WebSocket message the
previous standalone studio UI used, so the unified UI can implement the same
features. Everything here needs an **admin** principal (session cookie or API
key): studio projects are Python programs the daemon runs.

## Workspace and roots

- Projects live in the `video.workspace` setting (default `<data dir>/video`);
  each project is a folder with `project.py` (and the `project.json`
  snapshot), `assets/`, `media/`, `cache/` and `renders/<job_id>/`.
- `video.mediaLibrary` (optional) is offered as the shared media library.
- The `hello` event reports both paths.

## Deep links

`<harness>/studio?project=<project folder>&render=<job_id>` opens the studio
on a project with one render selected. `eks-harness studio publish` and
`eks_harness.studio.publish.publish()` print these links; `eks-harness studio
link <project> [--render <job>]` builds one.

## HTTP routes

All paths are relative to `/api/studio`. URLs the server hands out (render
outputs, thumbnails, preview frames, effect previews, media) already carry the
prefix.

| Method | Path | Purpose |
| --- | --- | --- |
| GET, HEAD | `/files/projects/{project_id}/{path}` | Any file inside a project (renders, thumbnails, `cache/preview_frames/<job>/<frame>.jpg`, media). Path traversal is refused with 404. Supports range requests (video seeking). |
| POST | `/files/projects/{project_id}/upload?kind=video\|audio\|image&filename=&overwrite=` | Multipart upload (`file` part) into `<project>/media/` (1 GiB limit); replies `{url, path}`; 409 when the file exists and `overwrite` is off, 413 above the size limit. Broadcasts `media.changed`. Cookie sessions send `X-CSRF-Token`. |
| GET, HEAD | `/preview-cache/{kind}/{name}` | Cached effect or transition preview clips (`kind` is `effects` or `transitions`). |
| GET, HEAD | `/sample/{kind}` | The generated sample clips previews are rendered from. |
| WebSocket | `/ws` | The studio channel (below). |

Misses answer `{"error": "not_found", "path": ...}` with 404; auth failures
answer 401 (`unauthorized`) or 403 (`admin_required`, `csrf_failed`).

## WebSocket protocol

### Envelopes

- Client requests: `{"type": "<request>", "id": "<correlation id>", "payload": {...}}`.
- Replies: `{"type": "<request>.reply", "id": "<same id>", "result": {...}}`.
- Errors: `{"type": "error", "id": "<id or null>", "code": "...", "message": "...", "detail": {...}}`
  (codes: `bad_request`, `bad_kind`, `not_found`, `exists`, `conflict`, `permission_denied`,
  `project_load_error`, `io_error`, `handler_error`, `server_error`, `not_implemented`).
- Server events: `{"type": "<event>", "payload": {...}}`, delivered to clients
  subscribed to the event's topic (plus `hello`, sent on connect).

On connect the server sends `hello`; the client then subscribes to topics.
The handshake is refused (close code 1008) without an admin principal, or with
a cookie session from another origin.

### Topics

| Topic | Events |
| --- | --- |
| `system` | `system.snapshot` (CPU, memory, GPU, temperatures, render load; about once a second), `system.error` |
| `projects` | `project.list`, `project.state`, `project.changed` (declared; the old server never emitted them) |
| `catalog` | `catalog.effects`, `catalog.transitions`, `catalog.preview_progress`, `catalog.preview_ready` |
| `media` | `media.list`, `media.changed` |
| `jobs` | `job.created`, `job.progress`, `job.event`, `job.preview_frame`, `job.succeeded`, `job.failed`, `job.cancelled` |
| `log` | `log` |
| `process_logs` | `process.log` (the daemon's own log records) |

### Replies by request

| Request | Reply `result` |
| --- | --- |
| `subscribe`, `unsubscribe` | `{topics}`: the client's topics afterwards |
| `ping` | `{ts}`; also a `pong` event |
| `projects.list` | `{projects: [{id, name, path}]}` filtered by `query` |
| `project.select`, `project.refresh` | declared in the protocol but never implemented (reply `not_implemented`); the old UI tracked the selected project itself |
| `project.timeline` | the resolved timeline (tracks, segments with resolved times, markers) |
| `render.start` | `{job_id, project_id, ...}`; progress arrives on `jobs` |
| `render.cancel` | `{ok, job_id, cancelled, status?}` |
| `catalog.request` | `{entries, kind}`; the `catalog.*` events follow |
| `preview.request_effect`, `preview.request_transition` | `{url, key}` when cached, else `{pending: true, request_id}` and later `catalog.preview_progress` / `catalog.preview_ready` |
| `preview.cancel` | `{ok, cancelled}` |
| `media.list` | `{scope, kind, entries}` |
| `media.upload` | `{url, path}` (base64 body for small files; use the HTTP upload for large ones) |
| `media.delete` | `{ok}` |
| `media.rename` | `{url, path}` |
| `artifacts.list` | `{project_id, artifacts: [{job_id, mode, status, started_at, finished_at, duration_s, output_url, thumbnail_url, total_frames, error_category, failure_summary}]}` |
| `artifacts.metadata` | `{project_id, job_id, metadata}`: the render's `metadata.json` (record, condensed events, settings) |
| `process.logs` | `{entries, attached}` (backlog of `process.log` records) |

## Message reference

Generated from `eks_harness.studio.dev.ws_protocol` (the models are the source
of truth; field names are snake_case on the wire).

### Client to server requests

#### `subscribe`

- `payload`: `SubscribePayload`
  - `topics`: `list[str]`

#### `unsubscribe`

- `payload`: `SubscribePayload`
  - `topics`: `list[str]`

#### `project.select`

- `payload`: `ProjectSelectPayload`
  - `project_id`: `str`

#### `project.refresh`

- `payload`: `ProjectSelectPayload`
  - `project_id`: `str`

#### `projects.list`

- `payload`: `ProjectsListPayload` = ProjectsListPayload(query='')
  - `query`: `str` = ''

#### `render.start`

- `payload`: `RenderStartPayload`
  - `project_id`: `str`
  - `mode`: `Literal['preview', 'final']` = 'preview'
  - `preset`: `str | None` = None
  - `encoder`: `str | None` = None
  - `bitrate_kbps`: `int | None` = None
  - `crf`: `int | None` = None
  - `width`: `int | None` = None
  - `height`: `int | None` = None
  - `range_start`: `float | None` = None
  - `range_end`: `float | None` = None

#### `render.cancel`

- `payload`: `RenderCancelPayload`
  - `job_id`: `str`

#### `catalog.request`

- `payload`: `CatalogRequestPayload`
  - `kind`: `Literal['effect', 'transition']`

#### `preview.request_effect`

- `payload`: `PreviewRequestPayload`
  - `name`: `str`
  - `params`: `dict[str, Any] | None` = None

#### `preview.request_transition`

- `payload`: `PreviewRequestPayload`
  - `name`: `str`
  - `params`: `dict[str, Any] | None` = None

#### `preview.cancel`

- `payload`: `PreviewCancelPayload`
  - `kind`: `Literal['effect', 'transition']`
  - `name`: `str`
  - `request_id`: `str | None` = None

#### `media.list`

- `payload`: `MediaListPayload`
  - `scope`: `Literal['project', 'global']` = 'project'
  - `kind`: `Optional[Literal['video', 'audio', 'image']]` = None
  - `project_id`: `str | None` = None

#### `artifacts.list`

- `payload`: `ArtifactsListPayload`
  - `project_id`: `str`

#### `artifacts.metadata`

- `payload`: `ArtifactsMetadataPayload`
  - `project_id`: `str`
  - `job_id`: `str`

#### `project.timeline`

- `payload`: `ProjectTimelinePayload`
  - `project_id`: `str`

#### `ping`

- `payload`: `PingPayload`
  - `ts`: `float | None` = None

#### `media.upload`

- `payload`: `MediaUploadPayload`
  - `project_id`: `str`
  - `kind`: `Literal['video', 'audio', 'image']`
  - `filename`: `str`
  - `data_base64`: `str`
  - `overwrite`: `bool` = False

#### `media.delete`

- `payload`: `MediaDeletePayload`
  - `project_id`: `str`
  - `path`: `str`

#### `media.rename`

- `payload`: `MediaRenamePayload`
  - `project_id`: `str`
  - `path`: `str`
  - `new_name`: `str`

#### `process.logs`

- `payload`: `ProcessLogsPayload`
  - `since_ts`: `float | None` = None
  - `limit`: `int` = 512

### Server to client events

#### `hello`

- `payload`: `HelloPayload`
  - `server`: `str`
  - `version`: `str`
  - `project_workspace`: `str | None` = None
  - `media_library_root`: `str | None` = None

#### `system.snapshot`

- `payload`: `SystemSnapshot`
  - `ts`: `float`
  - `cpu_pct`: `float`
  - `mem_pct`: `float`
  - `gpu_pct`: `float | None` = None
  - `cpu_temp_c`: `float | None` = None
  - `gpu_temp_c`: `float | None` = None
  - `render_pct`: `float | None` = None

#### `system.error`

- `payload`: `SystemErrorPayload`
  - `code`: `str`
  - `message`: `str`
  - `detail`: `dict[str, Any] | None` = None

#### `project.list`

- `payload`: `ProjectListPayload`
  - `projects`: `list[ProjectListItem]`

#### `project.state`

- `payload`: `ProjectStatePayload`
  - `project_id`: `str`
  - `project`: `dict[str, Any]`

#### `project.changed`

- `payload`: `ProjectChangedPayload`
  - `project_id`: `str`
  - `rev`: `int`

#### `catalog.effects`

- `payload`: `CatalogEntriesPayload`
  - `entries`: `list[CatalogEntry]`

#### `catalog.transitions`

- `payload`: `CatalogEntriesPayload`
  - `entries`: `list[CatalogEntry]`

#### `catalog.preview_ready`

- `payload`: `CatalogPreviewReadyPayload`
  - `kind`: `Literal['effect', 'transition']`
  - `name`: `str`
  - `url`: `str`
  - `key`: `str`

#### `catalog.preview_progress`

- `payload`: `CatalogPreviewProgressPayload`
  - `kind`: `Literal['effect', 'transition']`
  - `name`: `str`
  - `request_id`: `str`
  - `phase`: `Literal['queued', 'rendering', 'encoding', 'done', 'failed']`
  - `queue_position`: `int | None` = None
  - `progress`: `float | None` = None
  - `message`: `str | None` = None

#### `media.list`

- `payload`: `MediaListEventPayload`
  - `scope`: `Literal['project', 'global']`
  - `kind`: `Optional[Literal['video', 'audio', 'image']]` = None
  - `items`: `list[MediaItem]`

#### `job.created`

- `payload`: `JobRecordSnapshot`
  - `job_id`: `str`
  - `project_id`: `str`
  - `project_path`: `str`
  - `mode`: `str`
  - `status`: `str`
  - `progress`: `float` = 0.0
  - `message`: `str` = ''
  - `output_path`: `str | None` = None
  - `error`: `str | None` = None
  - `started_at`: `float`
  - `updated_at`: `float`
  - `current_step`: `str | None` = None
  - `step_index`: `int | None` = None
  - `total_steps`: `int` = 0
  - `frame_index`: `int` = 0
  - `frame_total`: `int` = 0
  - `eta_s`: `float | None` = None
  - `error_category`: `str | None` = None

#### `job.progress`

- `payload`: `JobRecordSnapshot`
  - `job_id`: `str`
  - `project_id`: `str`
  - `project_path`: `str`
  - `mode`: `str`
  - `status`: `str`
  - `progress`: `float` = 0.0
  - `message`: `str` = ''
  - `output_path`: `str | None` = None
  - `error`: `str | None` = None
  - `started_at`: `float`
  - `updated_at`: `float`
  - `current_step`: `str | None` = None
  - `step_index`: `int | None` = None
  - `total_steps`: `int` = 0
  - `frame_index`: `int` = 0
  - `frame_total`: `int` = 0
  - `eta_s`: `float | None` = None
  - `error_category`: `str | None` = None

#### `job.event`

- `payload`: `JobEventPayload`
  - `job_id`: `str`
  - `event`: `Literal['step_start', 'step_end', 'substep_start', 'substep_end', 'log', 'error']`
  - `data`: `dict[str, Any]`

#### `job.preview_frame`

- `payload`: `JobPreviewFramePayload`
  - `job_id`: `str`
  - `frame`: `int`
  - `url`: `str`
  - `t_s`: `float | None` = None

#### `job.succeeded`

- `payload`: `JobSucceededPayload`
  - `job_id`: `str`
  - `project_id`: `str`
  - `project_path`: `str`
  - `mode`: `str`
  - `status`: `str`
  - `progress`: `float` = 0.0
  - `message`: `str` = ''
  - `output_path`: `str | None` = None
  - `error`: `str | None` = None
  - `started_at`: `float`
  - `updated_at`: `float`
  - `current_step`: `str | None` = None
  - `step_index`: `int | None` = None
  - `total_steps`: `int` = 0
  - `frame_index`: `int` = 0
  - `frame_total`: `int` = 0
  - `eta_s`: `float | None` = None
  - `error_category`: `str | None` = None
  - `artifact`: `Artifact | None` = None

#### `job.failed`

- `payload`: `JobFailedPayload`
  - `job_id`: `str`
  - `project_id`: `str`
  - `project_path`: `str`
  - `mode`: `str`
  - `status`: `str`
  - `progress`: `float` = 0.0
  - `message`: `str` = ''
  - `output_path`: `str | None` = None
  - `error`: `str | None` = None
  - `started_at`: `float`
  - `updated_at`: `float`
  - `current_step`: `str | None` = None
  - `step_index`: `int | None` = None
  - `total_steps`: `int` = 0
  - `frame_index`: `int` = 0
  - `frame_total`: `int` = 0
  - `eta_s`: `float | None` = None
  - `error_category`: `str | None` = None
  - `failure_summary`: `dict[str, Any] | None` = None

#### `job.cancelled`

- `payload`: `JobRecordSnapshot`
  - `job_id`: `str`
  - `project_id`: `str`
  - `project_path`: `str`
  - `mode`: `str`
  - `status`: `str`
  - `progress`: `float` = 0.0
  - `message`: `str` = ''
  - `output_path`: `str | None` = None
  - `error`: `str | None` = None
  - `started_at`: `float`
  - `updated_at`: `float`
  - `current_step`: `str | None` = None
  - `step_index`: `int | None` = None
  - `total_steps`: `int` = 0
  - `frame_index`: `int` = 0
  - `frame_total`: `int` = 0
  - `eta_s`: `float | None` = None
  - `error_category`: `str | None` = None

#### `log`

- `payload`: `LogEntry`
  - `ts`: `float`
  - `level`: `str`
  - `source`: `str`
  - `message`: `str`
  - `payload`: `dict[str, Any] | None` = None

#### `error`

- `code`: `str`
- `message`: `str`
- `detail`: `dict[str, Any] | None` = None

#### `pong`

- `payload`: `PongPayload`
  - `ts`: `float`

#### `media.changed`

- `payload`: `MediaChangedPayload`
  - `project_id`: `str`

#### `process.log`

- `payload`: `ProcessLogEventPayload`
  - `entries`: `list[ProcessLogEntry]`

