"""M2 dev-server handlers: ``render.start`` + ``render.cancel``.

The dev server invokes the existing render pipeline directly (no MCP RPC
round-trip), so renders triggered from the browser look identical to
renders triggered by an MCP tool call - same registry, same subprocess,
same metadata.json artifact.

``render.start`` returns immediately with the new ``job_id`` and lets the
render run as a background asyncio task. Progress flows through the
:mod:`eks_harness.studio.dev.render_bridge` to the ``jobs`` topic.

``render.cancel`` reuses the canonical subprocess-signalling logic from
:func:`eks_harness.studio.tools.cancel_job` so cancellation semantics line up
with the MCP path.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from ...jobs import get_registry
from ...roots import RootsManager, get_roots_manager
from ...toolimpl import render_project as render_tool
from ..ws_hub import WSHub
from ..ws_protocol import (
    ErrorEnvelope,
    RenderCancelRequest,
    RenderStartRequest,
    ReplyEnvelope,
)
from .. import render_bridge

__all__ = [
    "handle_render_start",
    "handle_render_cancel",
    "register",
]

_LOG = logging.getLogger(__name__)


class _MinimalCtx:
    """Stand-in for ``mcp.server.fastmcp.Context`` used by the dev render path.

    The render driver only touches three Context methods: ``info``,
    ``warning``, ``report_progress`` (plus ``ctx.session.send_resource_updated``).
    A dev-triggered render has no MCP request envelope to attach to, so we
    forward those calls into structured logs and let the WS hub carry user-
    visible progress instead.
    """

    def __init__(self) -> None:
        self.session = _MinimalSession()

    async def info(self, message: str) -> None:
        _LOG.info("[dev-render] %s", message)

    async def warning(self, message: str) -> None:
        _LOG.warning("[dev-render] %s", message)

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        return None


class _MinimalSession:
    """No-op session shim - the dev path doesn't subscribe to MCP resources."""

    async def send_resource_updated(self, **kwargs: Any) -> None:
        return None


def _resolve_roots() -> RootsManager:
    """Default-construct a roots manager so the dev path works without MCP."""

    return get_roots_manager()


async def handle_render_start(
    request: RenderStartRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    payload = request.payload
    project_path = Path(payload.project_id)
    if not project_path.is_absolute():
        # Treat the value as either a workspace-relative project id or an
        # absolute path; ``_resolve_render_plan`` already handles both shapes.
        pass

    roots = _resolve_roots()
    registry = get_registry()

    try:
        plan = render_tool._resolve_render_plan(
            project_path=project_path,
            mode=payload.mode,
            encoder=payload.encoder,
            bitrate_kbps=payload.bitrate_kbps,
            crf=payload.crf,
            width=payload.width,
            height=payload.height,
            roots=roots,
        )
    except FileNotFoundError as exc:
        return ErrorEnvelope(id=request.id, code="not_found", message=str(exc))
    except PermissionError as exc:
        return ErrorEnvelope(id=request.id, code="permission_denied", message=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return ErrorEnvelope(id=request.id, code="bad_request", message=str(exc))

    try:
        record = await registry.create(
            project_id=plan.project_id,
            project_path=plan.project_dir,
            mode=payload.mode,
        )
    except ValueError as exc:
        return ErrorEnvelope(id=request.id, code="conflict", message=str(exc))
    await registry.update(record.job_id, status="running", message="render started")

    # Push a job.created event up-front so subscribers see the new job before
    # the renderer subprocess produces its first stdout line.
    await render_bridge.broadcast_job_created(hub, record)

    # Spawn a registry watcher so every JobRegistry.update() also lands as a
    # job.progress event on the wire. The watcher stops itself when the job
    # reaches a terminal status.
    asyncio.create_task(
        render_bridge.subscribe_registry(hub, registry, record.job_id),
        name=f"dev-render-watch-{record.job_id}",
    )

    ctx = _MinimalCtx()
    preview_frames_dir = (
        plan.project_dir / "cache" / "preview_frames" / record.job_id
    )

    async def _run_render() -> None:
        try:
            await render_tool._drive_render(
                ctx=ctx,  # type: ignore[arg-type]
                registry=registry,
                job_id=record.job_id,
                project_dir=plan.project_dir,
                output_path=plan.output_path,
                mode=payload.mode,
                range_start=payload.range_start,
                range_end=payload.range_end,
                settings_overrides=plan.settings_overrides,
                preview_frames_dir=preview_frames_dir,
                hub=hub,
                project_id=plan.project_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("dev render %s crashed", record.job_id)

    task = asyncio.create_task(_run_render(), name=f"dev-render-{record.job_id}")
    render_tool._track_render_task(record.job_id, task)

    return ReplyEnvelope(
        type="render.start.reply",
        id=request.id,
        result={
            "job_id": record.job_id,
            "project_id": plan.project_id,
            "project_path": str(plan.project_dir),
            "output_path": str(plan.output_path),
            "mode": payload.mode,
        },
    )


async def handle_render_cancel(
    request: RenderCancelRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope | ErrorEnvelope:
    job_id = request.payload.job_id
    registry = get_registry()
    record = registry.get(job_id)
    if record is None:
        return ErrorEnvelope(
            id=request.id, code="not_found", message=f"unknown job {job_id}"
        )
    if record.status not in {"queued", "running"}:
        return ReplyEnvelope(
            type="render.cancel.reply",
            id=request.id,
            result={"ok": True, "job_id": job_id, "status": record.status, "cancelled": False},
        )

    proc = render_tool.get_subprocess(job_id)
    if proc is not None and proc.returncode is None:
        try:
            if sys.platform.startswith("win"):
                import signal as _signal

                proc.send_signal(_signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    await registry.update(
        job_id, status="cancelled", message="cancellation requested"
    )
    return ReplyEnvelope(
        type="render.cancel.reply",
        id=request.id,
        result={"ok": True, "job_id": job_id, "cancelled": True},
    )


def register(register_handler) -> None:
    """Wire the M2 render handlers into the endpoint dispatch registry."""

    register_handler("render.start", handle_render_start)
    register_handler("render.cancel", handle_render_cancel)
