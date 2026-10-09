"""``render_project`` tool.

Renders a project workspace via a subprocess pool. The renderer subprocess
emits one JSON-line per progress event on stdout; the parent forwards each
event to the caller's reporter (``report_progress``) and updates the job
registry, whose ``eks-harness://video/render_jobs/<id>`` resource and the
studio ``jobs`` topic carry the progress to subscribers.

:func:`render` returns a :class:`JobRef` dict immediately and drives the
render in a background asyncio task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Literal


from ..jobs import JobRegistry, get_registry
from ..roots import RootsManager
from ..types import JobRef, NullReporter, Reporter

__all__ = ["DESCRIPTION", "RENDER_DRIVER_MODULE", "render"]

_LOG = logging.getLogger(__name__)

RENDER_DRIVER_MODULE = "eks_harness.studio.render_driver"


async def _run(
    *,
    project_path: Path,
    mode: str,
    encoder: str | None,
    bitrate_kbps: int | None,
    crf: int | None,
    width: int | None = None,
    height: int | None = None,
    range_start: float | None,
    range_end: float | None,
    ctx: Reporter | None,
    roots: RootsManager,
    registry: JobRegistry,
) -> dict[str, Any]:
    ctx = ctx if ctx is not None else NullReporter()

    plan = _resolve_render_plan(
        project_path=project_path,
        mode=mode,
        encoder=encoder,
        bitrate_kbps=bitrate_kbps,
        crf=crf,
        width=width,
        height=height,
        roots=roots,
    )

    record = await registry.create(project_id=plan.project_id, project_path=plan.project_dir, mode=mode)
    await registry.update(record.job_id, status="running", message="render started")

    task = asyncio.create_task(
        _drive_render(
            ctx=ctx,
            registry=registry,
            job_id=record.job_id,
            project_dir=plan.project_dir,
            output_path=plan.output_path,
            mode=mode,
            range_start=range_start,
            range_end=range_end,
            settings_overrides=plan.settings_overrides,
            preview_frames_dir=plan.project_dir / "cache" / "preview_frames" / record.job_id,
        )
    )
    _track_render_task(record.job_id, task)

    ref = JobRef(
        uri=f"job://{record.job_id}",
        job_id=record.job_id,
        project_uri=plan.project_uri,
        status_uri=f"eks-harness://video/render_jobs/{record.job_id}",
    )
    return ref.model_dump(mode="json")


class _RenderPlan:
    __slots__ = (
        "project_dir",
        "project_id",
        "project_uri",
        "output_path",
        "settings_overrides",
    )

    def __init__(
        self,
        *,
        project_dir: Path,
        project_id: str,
        project_uri: str,
        output_path: Path,
        settings_overrides: dict[str, Any],
    ) -> None:
        self.project_dir = project_dir
        self.project_id = project_id
        self.project_uri = project_uri
        self.output_path = output_path
        self.settings_overrides = settings_overrides


def _resolve_render_plan(
    *,
    project_path: Path,
    mode: str,
    encoder: str | None,
    bitrate_kbps: int | None,
    crf: int | None,
    width: int | None = None,
    height: int | None = None,
    roots: RootsManager,
) -> _RenderPlan:
    workspace = roots.project_workspace()
    project_dir = project_path if project_path.is_absolute() else workspace / project_path
    project_dir = project_dir.resolve()
    if not (project_dir / "project.py").exists():
        raise FileNotFoundError(f"no project.py at {project_dir}")
    if not _is_under(project_dir, workspace):
        raise PermissionError(
            f"project {project_dir} is not under the granted project_workspace root ({workspace})"
        )

    project_id = project_dir.name
    project_uri = f"eks-harness://video/projects/{project_id}"
    output_path = project_dir / "renders" / _output_filename(mode)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    settings_overrides: dict[str, Any] = {}
    if encoder:
        settings_overrides["encoder"] = encoder
    if bitrate_kbps is not None:
        settings_overrides["bitrate_kbps"] = bitrate_kbps
    if crf is not None:
        settings_overrides["crf"] = crf
    # A custom resolution needs both axes - a lone width or height is
    # ambiguous, so only forward the override when the pair is complete. The
    # driver routes ``resolution`` onto ``project.resolution`` (it lives on the
    # Project, not RenderSettings).
    if width is not None and height is not None:
        settings_overrides["resolution"] = (width, height)

    return _RenderPlan(
        project_dir=project_dir,
        project_id=project_id,
        project_uri=project_uri,
        output_path=output_path,
        settings_overrides=settings_overrides,
    )


# Stable progress weighting shared with TqdmProgressReporter so the bar
# advertised over the MCP progress channel tracks the bar a human running
# the CLI directly sees. If the SDK ever rebalances these weights, this
# table should be updated in lockstep.
_STEP_WEIGHTS: dict[str, float] = {
    "load_project": 0.01,
    "extract_markers": 0.20,
    "plan_segments": 0.01,
    "render_segments": 0.72,
    "mux_audio": 0.04,
    "finalize": 0.02,
}


class _EventConsumer:
    """Translate render-driver JSON events into registry + MCP updates.

    Accepts both the legacy ``{"progress":..,"message":..}`` shape and the
    new event-tagged shape (``{"event":"step_start",...}`` etc.) so a
    transitional rollout does not require both sides to ship together.

    When ``hub`` is supplied (dev-server path), every non-``frame`` event is
    appended to :attr:`recorded_events` for the metadata.json log, and every
    event is mirrored onto the WebSocket hub via
    :mod:`eks_harness.studio.dev.render_bridge`.
    """

    def __init__(
        self,
        *,
        registry: JobRegistry,
        job_id: str,
        ctx: Reporter,
        task_ctx: Any = None,
        hub: Any = None,
        project_id: str | None = None,
    ) -> None:
        self._registry = registry
        self._job_id = job_id
        self._ctx = ctx
        self._task_ctx = task_ctx
        self._completed_weight = 0.0
        self._current_step: str | None = None
        self._frame_total = 0
        self._last_progress = 0.0
        self.cancelled = False
        self.error_message: str | None = None
        self.error_category: str | None = None
        # Dev-server wiring - both None in the legacy / task paths.
        self._hub = hub
        self._project_id = project_id
        # Persisted event log written to metadata.json on terminal state.
        # ``frame`` events are excluded (high volume, low signal); the rest
        # of the event stream is captured verbatim.
        self.recorded_events: list[dict[str, Any]] = []

    async def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        # Record the event for the post-render metadata log before
        # dispatching, so even short-circuits below don't drop entries.
        if kind is not None and kind != "frame":
            try:
                self.recorded_events.append(dict(event))
            except Exception:  # pragma: no cover - defensive
                pass
        # Mirror onto the dev-server hub. This is a no-op when hub is None.
        if self._hub is not None and self._project_id is not None:
            try:
                from ..dev import render_bridge  # lazy: avoid circular import

                render_bridge.attach_to_event_consumer(
                    self,
                    hub=self._hub,
                    job_id=self._job_id,
                    project_id=self._project_id,
                    event=event,
                )
            except Exception:  # pragma: no cover - bridge must not break renders
                _LOG.debug("render_bridge forward failed", exc_info=True)
        if kind is None:
            await self._handle_legacy(event)
            return
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            return
        await handler(event)

    async def _handle_legacy(self, event: dict[str, Any]) -> None:
        progress = float(event.get("progress", self._last_progress))
        self._last_progress = progress
        message = str(event.get("message", ""))
        await self._update(progress=progress, message=message)
        await self._notify_progress(progress, message=message)
        if message:
            await self._task_status(message)

    async def _on_step_start(self, event: dict[str, Any]) -> None:
        step_name = str(event.get("step", ""))
        total = int(event.get("total", 0)) or None
        # Orchestrator emits 0-based ``index`` values per progress.STEP_ORDER
        # (load_project=0 ... finalize=5). Propagate verbatim into the
        # JobRecord so the UI never has to fall back to "?" between the
        # ``job.event`` arrival and the snapshot push.
        raw_index = event.get("index")
        step_index = int(raw_index) if isinstance(raw_index, (int, float)) else None
        self._current_step = step_name
        self._frame_total = int(event.get("frame_total", 0))
        progress = self._completed_weight
        await self._update(
            progress=progress,
            message=f"step: {step_name}",
            current_step=step_name,
            step_index=step_index,
            total_steps=total,
            frame_total=self._frame_total or None,
        )
        await self._notify_progress(progress, message=step_name)
        await self._task_status(f"step: {step_name}")

    async def _on_step_end(self, event: dict[str, Any]) -> None:
        step_name = str(event.get("step", ""))
        self._completed_weight = min(
            1.0, self._completed_weight + _STEP_WEIGHTS.get(step_name, 0.0)
        )
        await self._update(progress=self._completed_weight)
        await self._notify_progress(self._completed_weight)

    async def _on_substep_start(self, event: dict[str, Any]) -> None:
        name = str(event.get("name", ""))
        step_name = str(event.get("step", ""))
        await self._task_status(f"{step_name}: {name}")

    async def _on_substep_end(self, event: dict[str, Any]) -> None:
        return None

    async def _on_frame(self, event: dict[str, Any]) -> None:
        frame_idx = int(event.get("frame_index", 0))
        frame_total = int(event.get("frame_total", self._frame_total or 0))
        fps = float(event.get("fps_observed", 0.0))
        eta = event.get("eta_s")
        within = (frame_idx / frame_total) if frame_total else 0.0
        weight = _STEP_WEIGHTS.get("render_segments", 0.0)
        progress = min(1.0, self._completed_weight + within * weight)
        await self._update(
            progress=progress,
            frame_index=frame_idx,
            frame_total=frame_total or None,
            eta_s=float(eta) if eta is not None else None,
            message=f"frame {frame_idx}/{frame_total} @ {fps:.1f} fps",
        )
        await self._notify_progress(
            progress, message=f"{frame_idx}/{frame_total} @ {fps:.1f} fps"
        )

    async def _on_log(self, event: dict[str, Any]) -> None:
        level = str(event.get("level", "info")).lower()
        message = str(event.get("message", ""))
        if not message:
            return
        info = getattr(self._ctx, "info", None)
        warning = getattr(self._ctx, "warning", None)
        try:
            if level == "warning" and warning is not None:
                await warning(message)
            elif info is not None:
                await info(message)
        except Exception:
            _LOG.debug("log forwarding failed", exc_info=True)

    async def _on_done(self, event: dict[str, Any]) -> None:
        await self._update(progress=1.0, message=str(event.get("output", "")) or None)
        await self._notify_progress(1.0)

    async def _on_error(self, event: dict[str, Any]) -> None:
        self.error_message = str(event.get("message", "")) or None
        self.error_category = str(event.get("category", "")) or None

    async def _on_cancelled(self, event: dict[str, Any]) -> None:
        self.cancelled = True
        self.error_category = "cancelled"

    async def _update(
        self,
        *,
        progress: float | None = None,
        message: str | None = None,
        current_step: str | None = None,
        step_index: int | None = None,
        total_steps: int | None = None,
        frame_index: int | None = None,
        frame_total: int | None = None,
        eta_s: float | None = None,
    ) -> None:
        if progress is not None:
            self._last_progress = progress
        await self._registry.update(
            self._job_id,
            progress=progress,
            message=message,
            current_step=current_step,
            step_index=step_index,
            total_steps=total_steps,
            frame_index=frame_index,
            frame_total=frame_total,
            eta_s=eta_s,
        )

    async def _notify_progress(self, progress: float, *, message: str | None = None) -> None:
        try:
            await self._ctx.report_progress(progress, total=1.0, message=message or None)
            await self._ctx.session.send_resource_updated(uri=_status_uri_for(self._job_id))
        except Exception:
            _LOG.debug("progress notification failed (likely not subscribed)", exc_info=True)

    async def _task_status(self, message: str) -> None:
        if self._task_ctx is None or not message:
            return
        await _safe_update_status(self._task_ctx, message)


async def _drive_render(
    *,
    ctx: Reporter,
    registry: JobRegistry,
    job_id: str,
    project_dir: Path,
    output_path: Path,
    mode: str,
    range_start: float | None,
    range_end: float | None,
    settings_overrides: dict[str, Any],
    preview_frames_dir: Path,
    task_ctx: Any = None,
    task_store: Any = None,
    return_outcome: bool = False,
    hub: Any = None,
    project_id: str | None = None,
) -> tuple[bool, str | None] | None:
    """Spawn the render driver subprocess and stream its progress lines.

    When ``return_outcome`` is True the function returns a ``(success, error)``
    tuple instead of None - used by the task-augmented path so it can fail the
    task with a useful message.
    """

    preview_frames_dir.mkdir(parents=True, exist_ok=True)

    # Nest the output under ``renders/<job_id>/`` so each render owns a
    # dedicated directory for its mp4 + metadata.json + thumbnail.jpg. The
    # artifact scanner walks ``renders/*`` looking for metadata.json files,
    # and the dev-server file routes serve everything beneath
    # ``/files/projects/<project_id>/renders/<job_id>/``.
    if output_path.parent.name != job_id:
        output_path = output_path.parent / job_id / output_path.name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        RENDER_DRIVER_MODULE,
        "--project-dir", str(project_dir),
        "--output", str(output_path),
        "--mode", mode,
        "--preview-frames-dir", str(preview_frames_dir),
    ]
    if range_start is not None:
        cmd.extend(["--range-start", str(range_start)])
    if range_end is not None:
        cmd.extend(["--range-end", str(range_end)])
    if settings_overrides:
        cmd.extend(["--settings", json.dumps(settings_overrides)])

    started = time.monotonic()
    spawn_kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if sys.platform.startswith("win"):
        # Put the renderer in its own process group so CTRL_BREAK_EVENT (used by
        # tasks/cancel) targets only the renderer and never the parent server
        # process or the test runner.
        from subprocess import CREATE_NEW_PROCESS_GROUP

        spawn_kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, **spawn_kwargs)
    except FileNotFoundError as exc:
        err = f"failed to spawn render: {exc}"
        await registry.update(job_id, status="failed", error=err)
        return (False, err) if return_outcome else None

    _track_subprocess(job_id, proc)
    if task_store is not None:
        task_store.register_subprocess(job_id, proc)

    # Drain stderr concurrently into a buffer. Without this, the renderer
    # subprocess deadlocks the moment third-party libraries (librosa,
    # numba, torch, matplotlib) emit enough warnings to fill the OS pipe
    # buffer (~4-8 KB on Windows) - the child blocks on stderr.write
    # forever, never closes stdout, and the read loop below hangs forever.
    stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        async for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(_drain_stderr())

    consumer = _EventConsumer(
        registry=registry,
        job_id=job_id,
        ctx=ctx,
        task_ctx=task_ctx,
        hub=hub,
        project_id=project_id,
    )
    assert proc.stdout is not None
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                _LOG.debug("render_driver emitted non-JSON line: %s", line[:200])
                continue
            await consumer.handle(event)

        return_code = await proc.wait()
        duration = time.monotonic() - started
        await stderr_task
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        if return_code == 0 and output_path.exists():
            await registry.update(
                job_id,
                status="succeeded",
                progress=1.0,
                message=f"render complete in {duration:.1f}s",
                output_path=output_path.as_uri(),
            )
            await _finalize_artifacts(
                job_id=job_id,
                registry=registry,
                output_path=output_path,
                consumer=consumer,
                hub=hub,
                project_id=project_id,
                with_thumbnail=True,
            )
            return (True, None) if return_outcome else None
        if consumer.cancelled or return_code in (-15, 143, -9, 137, -2, 130):
            await registry.update(
                job_id,
                status="cancelled",
                message="render cancelled",
                error_category="cancelled",
            )
            await _finalize_artifacts(
                job_id=job_id,
                registry=registry,
                output_path=output_path,
                consumer=consumer,
                hub=hub,
                project_id=project_id,
                with_thumbnail=False,
            )
            return (False, "render cancelled") if return_outcome else None
        err = consumer.error_message
        if err is None:
            err = stderr_text.splitlines()[-1] if stderr_text.strip() else f"render exited {return_code}"
        await registry.update(
            job_id,
            status="failed",
            error=err,
            error_category=consumer.error_category,
        )
        await _finalize_artifacts(
            job_id=job_id,
            registry=registry,
            output_path=output_path,
            consumer=consumer,
            hub=hub,
            project_id=project_id,
            with_thumbnail=False,
        )
        return (False, err) if return_outcome else None
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task
        if task_store is not None:
            task_store.unregister_subprocess(job_id)


async def _finalize_artifacts(
    *,
    job_id: str,
    registry: JobRegistry,
    output_path: Path,
    consumer: "_EventConsumer",
    hub: Any,
    project_id: str | None,
    with_thumbnail: bool,
) -> None:
    """Write the metadata.json + thumbnail.jpg and broadcast the terminal hub event.

    Best-effort: any failure here is logged but doesn't propagate, so a broken
    ffmpeg or a read-only renders/ dir can't fail the render itself.
    """

    record = registry.get(job_id)
    if record is None:  # pragma: no cover - registry guarantee
        return

    # Lazy imports keep ``tools.render_project`` importable without the dev
    # extras (no ws_protocol, no artifacts module needed for stdio renders).
    try:
        from .. import artifacts as artifacts_mod
    except Exception:  # pragma: no cover - defensive
        _LOG.debug("artifacts module import failed", exc_info=True)
        return

    try:
        artifacts_mod.write_metadata(
            output_path=output_path,
            record=record,
            condensed_events=list(consumer.recorded_events),
        )
    except Exception:  # pragma: no cover - never fail the render on metadata io
        _LOG.debug("write_metadata failed for %s", job_id, exc_info=True)

    # For the terminal-event broadcast we need the same failure_summary that
    # write_metadata persisted. Recompute it here so the hub event matches
    # the on-disk metadata.json exactly (the scrape is cheap).
    failure_summary: dict[str, Any] | None = None
    if record.status == "failed":
        try:
            failure_summary = artifacts_mod._scrape_failure_summary(
                list(consumer.recorded_events), record.to_dict()
            )
        except Exception:  # pragma: no cover - defensive
            _LOG.debug("scrape_failure_summary failed for %s", job_id, exc_info=True)

    thumbnail_path: Path | None = None
    if with_thumbnail and output_path.exists():
        try:
            thumbnail_path = await artifacts_mod.extract_thumbnail_async(
                output_path=output_path
            )
        except Exception:  # pragma: no cover - thumbnail is nice-to-have
            _LOG.debug("extract_thumbnail failed for %s", job_id, exc_info=True)

    if hub is None or project_id is None:
        return

    try:
        from ..dev import render_bridge

        artifact = artifacts_mod.build_artifact(
            project_id=project_id,
            job_id=job_id,
            record_dict=record.to_dict(),
            output_path=output_path if output_path.exists() else None,
            thumbnail_path=thumbnail_path,
            failure_summary=failure_summary,
        )
        await render_bridge.broadcast_terminal(
            hub, record, artifact=artifact, failure_summary=failure_summary
        )
        render_bridge.reset_preview_throttle(job_id)
    except Exception:  # pragma: no cover - dev-server side effect
        _LOG.debug("terminal hub broadcast failed for %s", job_id, exc_info=True)


async def _safe_update_status(task_ctx: Any, message: str) -> None:
    try:
        await task_ctx.update_status(message)
    except Exception:
        _LOG.debug("task status notification failed", exc_info=True)


def _output_filename(mode: str) -> str:
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{mode}-{stamp}.mp4"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_TASKS: dict[str, asyncio.Task[Any]] = {}


def _track_subprocess(job_id: str, proc: asyncio.subprocess.Process) -> None:
    _PROCESSES[job_id] = proc


def _track_render_task(job_id: str, task: asyncio.Task[Any]) -> None:
    """Hold a strong reference to the background render task.

    asyncio.create_task only weak-refs its task; without a strong ref the
    garbage collector can cancel mid-render. We drop the reference once the
    task completes via add_done_callback.
    """

    _TASKS[job_id] = task
    task.add_done_callback(lambda _t: _TASKS.pop(job_id, None))


def get_subprocess(job_id: str) -> asyncio.subprocess.Process | None:
    return _PROCESSES.get(job_id)


def _status_uri_for(job_id: str) -> str:
    from pydantic import AnyUrl

    return str(AnyUrl(f"eks-harness://video/render_jobs/{job_id}"))


DESCRIPTION = (
    "Render a video project. `mode=preview` swaps in lightweight quality profiles for fast feedback; "
    "`mode=final` produces the deliverable. Returns a job reference at once; progress is reported to the "
    "caller and published on the eks-harness://video/render_jobs/<id> resource."
)


async def render(
    project_path: str | Path,
    mode: Literal["preview", "final"] = "preview",
    *,
    encoder: str | None = None,
    bitrate_kbps: int | None = None,
    crf: int | None = None,
    width: int | None = None,
    height: int | None = None,
    range_start: float | None = None,
    range_end: float | None = None,
    reporter: Reporter | None = None,
    roots: RootsManager | None = None,
    registry: JobRegistry | None = None,
) -> dict[str, Any]:
    from ..roots import get_roots_manager

    return await _run(project_path=Path(project_path), mode=mode, encoder=encoder, bitrate_kbps=bitrate_kbps,
                      crf=crf, width=width, height=height, range_start=range_start, range_end=range_end,
                      ctx=reporter, roots=roots or get_roots_manager(), registry=registry or get_registry())
