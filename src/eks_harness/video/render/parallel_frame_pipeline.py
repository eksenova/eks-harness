"""Chunked, multi-process frame pipeline.

A segment whose frame tail is composed entirely of ``parallel_safe``
processors produces identical output for a frame regardless of which frames
were rendered before it. Such a segment can therefore be split into
contiguous frame ranges that are rendered concurrently by separate worker
processes and then concatenated.

Workers do **not** receive pickled processors or a :class:`RenderContext` -
those hold ONNX / torch / PyAV handles that are not reliably picklable across
a spawn boundary. Instead each worker receives plain primitives plus the path
to the project's ``project.py`` and the project's resolved markers, rebuilds
the segment plan through the ordinary orchestrator code path (so the
processor chain is bit-identical to the serial run), and renders only its
window using the global frame index ``frame_start + i`` for every frame. That
global index is the determinism crux: glitch/shake/film-grain and friends
seed their RNG from ``frame_idx``/``t``, so feeding the global index makes a
chunk's pixels identical to the matching slice of the serial render.

The parent splits the frame total into ``workers`` contiguous ranges, fans
the chunks out across a :class:`ProcessPoolExecutor` (spawn on Windows),
concatenates the encoded chunk files with the ffmpeg concat demuxer, and
cleans up the temporary chunk files. Cancellation tears the pool down and
removes any chunk files already written.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .mux import mux_segments
from .progress import ProgressEvent, ProgressReporter

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = [
    "DEFAULT_MAX_WORKERS",
    "MIN_CHUNK_FRAMES",
    "ParallelFramePipelineError",
    "ChunkSpec",
    "render_segment_parallel",
    "resolve_worker_count",
    "split_frame_ranges",
]

_LOG = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 4
MIN_CHUNK_FRAMES = 48
_WORKERS_ENV = "EKS_HARNESS_RENDER_WORKERS"


class ParallelFramePipelineError(RuntimeError):
    """Raised when a chunk worker fails so the orchestrator can fall back."""


@dataclass(frozen=True)
class ChunkSpec:
    """Picklable description of one frame-range chunk handed to a worker.

    Only primitives / strings / paths cross the spawn boundary. ``markers``
    is the project's resolved marker streams as plain ``{stream: [float]}``
    dictionaries so workers reproduce animated parameters without re-running
    (potentially non-deterministic, always expensive) marker extraction.
    """

    project_path: str
    segment_id: str
    output_path: str
    frame_start: int
    frame_count: int
    fps: float
    width: int
    height: int
    duration: float
    ffmpeg_binary: str
    resolution: tuple[int, int]
    marker_streams: dict[str, list[float]]
    marker_named: dict[str, list[float]]
    render_settings: dict[str, object]


def resolve_worker_count(frame_total: int) -> int:
    """Effective worker count, honouring ``EKS_HARNESS_RENDER_WORKERS``.

    Defaults to ``min(os.cpu_count(), DEFAULT_MAX_WORKERS)`` and is clamped so
    every chunk holds at least :data:`MIN_CHUNK_FRAMES` frames. Returns ``1``
    when parallelism would not help (the caller then takes the serial path).
    """

    override = os.environ.get(_WORKERS_ENV)
    if override is not None and override.strip():
        try:
            requested = int(override)
        except ValueError:
            requested = 0
        if requested <= 0:
            return 1
        cap = requested
    else:
        cap = min(os.cpu_count() or 1, DEFAULT_MAX_WORKERS)
    by_frames = max(1, frame_total // MIN_CHUNK_FRAMES)
    return max(1, min(cap, by_frames))


def split_frame_ranges(frame_total: int, workers: int) -> list[tuple[int, int]]:
    """Partition ``frame_total`` into ``workers`` contiguous ``(start, count)``.

    The base size is ``frame_total // workers``; the first ``remainder``
    chunks each take one extra frame so the ranges tile ``[0, frame_total)``
    exactly with no gap or overlap. Empty chunks are never emitted.
    """

    if workers <= 1 or frame_total <= 0:
        return [(0, frame_total)]
    base, remainder = divmod(frame_total, workers)
    ranges: list[tuple[int, int]] = []
    start = 0
    for i in range(workers):
        count = base + (1 if i < remainder else 0)
        if count <= 0:
            continue
        ranges.append((start, count))
        start += count
    return ranges


def render_segment_parallel(
    *,
    project_path: Path,
    plan: object,
    ctx: RenderContext,
    output_path: Path,
    frame_total: int,
    workers: int,
    ffmpeg_binary: str,
    chunk_dir: Path,
) -> Path:
    """Render ``plan`` across ``workers`` processes into ``output_path``.

    Returns the concatenated segment file. Raises
    :class:`ParallelFramePipelineError` on any worker failure so the caller
    can decide whether to fall back to the serial pipeline. Honours the
    cooperative cancel event reachable via ``ctx.extra['cancel']``.
    """

    from eks_harness.video.render.orchestrator import _SegmentPlan  # local import: cycle

    assert isinstance(plan, _SegmentPlan)

    ranges = split_frame_ranges(frame_total, workers)
    width, height = ctx.project.resolution
    reporter = _resolve_reporter(ctx)
    cancel = _resolve_cancel(ctx)
    segment_id = plan.segment.id

    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_paths: list[Path] = []
    chunk_specs: list[ChunkSpec] = []
    for index, (start, count) in enumerate(ranges):
        chunk_path = chunk_dir / f"{segment_id}.chunk{index:04d}.mp4"
        chunk_paths.append(chunk_path)
        chunk_specs.append(
            ChunkSpec(
                project_path=str(project_path),
                segment_id=segment_id,
                output_path=str(chunk_path),
                frame_start=start,
                frame_count=count,
                fps=float(ctx.project.fps),
                width=width,
                height=height,
                duration=plan.duration,
                ffmpeg_binary=ffmpeg_binary,
                resolution=(width, height),
                marker_streams={k: list(v) for k, v in ctx.markers.streams.items()},
                marker_named={k: list(v) for k, v in ctx.markers.named.items()},
                render_settings=ctx.project.render_settings.model_dump(),
            )
        )

    emit_log = _make_log_emitter(reporter, segment_id)
    emit_log(
        f"parallel frame pipeline: {len(chunk_specs)} chunks x ~{ranges[0][1]} frames "
        f"({frame_total} total) across {workers} workers"
    )

    started = time.monotonic()
    completed_frames = 0
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_render_chunk, spec): spec for spec in chunk_specs}
            pending = set(futures)
            while pending:
                if cancel is not None and cancel.is_set():
                    _abort_pool(pool, futures)
                    raise _CancelledError()
                done, pending = wait(pending, timeout=0.25, return_when=FIRST_EXCEPTION)
                for future in done:
                    result = future.result()  # re-raises worker exceptions
                    completed_frames += result
                    _emit_chunk_progress(
                        reporter,
                        segment_id=segment_id,
                        frames_done=completed_frames,
                        frame_total=frame_total,
                        elapsed=time.monotonic() - started,
                    )
    except _CancelledError:
        _cleanup(chunk_paths)
        raise
    except Exception as exc:  # worker crash → surface for serial fallback
        _cleanup(chunk_paths)
        raise ParallelFramePipelineError(
            f"parallel chunk render failed for segment {segment_id!r}: {exc}"
        ) from exc

    try:
        mux_segments(chunk_paths, output_path, binary=ffmpeg_binary)
    finally:
        _cleanup(chunk_paths)

    emit_log(
        f"parallel frame pipeline: segment {segment_id!r} done in "
        f"{time.monotonic() - started:.2f}s ({frame_total} frames)"
    )
    return output_path


def _render_chunk(spec: ChunkSpec) -> int:
    """Worker entry point - render one frame-range chunk to its own file.

    Runs in a spawned subprocess. Rebuilds the project + markers + segment
    plan, opens the frame-tail processors and renders only
    ``[frame_start, frame_start + frame_count)`` with the global frame index.
    Returns the number of frames written so the parent can aggregate progress.
    """

    # Imports are inside the worker so the spawned interpreter performs them
    # after re-importing this module; keeps the picklable surface minimal.
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.plugins.registry import ensure_registry
    from eks_harness.video.render.context import RenderContext, RenderOptions
    from eks_harness.video.render.frame_pipeline import FramePipeline, FramePipelineSpec
    from eks_harness.video.render.orchestrator import Renderer
    from eks_harness.video.loader import load_project_file as _load_project

    ensure_registry()

    project = _load_project(Path(spec.project_path) / "project.py")
    project.resolution = spec.resolution
    for key, value in spec.render_settings.items():
        with contextlib.suppress(Exception):
            setattr(project.render_settings, key, value)

    markers = MarkerSet(
        streams={k: list(v) for k, v in spec.marker_streams.items()},
        named={k: list(v) for k, v in spec.marker_named.items()},
    )
    options = RenderOptions(
        output=Path(spec.output_path),
        workspace=Path(spec.project_path),
        ffmpeg_binary=spec.ffmpeg_binary,
    )
    renderer = Renderer(project, options)
    ctx = RenderContext(
        project=project,
        options=options,
        markers=markers,
        workspace=Path(spec.project_path),
        cache_dir=Path(spec.project_path) / "cache",
    )

    segment = _find_segment(project, spec.segment_id)
    plan = renderer._plan_segment(segment, ctx, markers)
    prefix_filter, processors, input_args, is_image, media_path = (
        renderer._frame_tail_runtime(plan, ctx)
    )

    chunk_duration = spec.frame_count / spec.fps
    pipeline_spec = FramePipelineSpec(
        input_path=media_path,
        output_path=Path(spec.output_path),
        width=spec.width,
        height=spec.height,
        fps=spec.fps,
        duration=chunk_duration,
        pix_fmt="bgr24",
        graph_prefix_filter=prefix_filter,
        input_args=input_args,
        ffmpeg_binary=spec.ffmpeg_binary,
        is_image=is_image,
        frame_start=spec.frame_start,
        frame_count=spec.frame_count,
    )
    result = FramePipeline(pipeline_spec, processors).run()
    return int(result.frames_written)


def _find_segment(project: object, segment_id: str) -> object:
    for track in project.tracks:  # type: ignore[attr-defined]
        for segment in track.segments:
            if segment.id == segment_id:
                return segment
    raise LookupError(f"segment {segment_id!r} not found in project")


def _resolve_reporter(ctx: RenderContext) -> ProgressReporter | None:
    reporter = ctx.extra.get("progress_reporter")
    if reporter is not None and hasattr(reporter, "emit"):
        return reporter  # type: ignore[return-value]
    return None


def _resolve_cancel(ctx: RenderContext) -> threading.Event | None:
    cancel = ctx.extra.get("cancel")
    if isinstance(cancel, threading.Event):
        return cancel
    return None


def _make_log_emitter(reporter: ProgressReporter | None, segment_id: str):
    def _emit(message: str) -> None:
        if reporter is None:
            return
        with contextlib.suppress(Exception):
            reporter.emit(
                ProgressEvent(
                    "log",
                    payload={"level": "info", "message": message, "segment": segment_id},
                )
            )

    return _emit


def _emit_chunk_progress(
    reporter: ProgressReporter | None,
    *,
    segment_id: str,
    frames_done: int,
    frame_total: int,
    elapsed: float,
) -> None:
    if reporter is None:
        return
    fps_observed = frames_done / elapsed if elapsed > 0 else 0.0
    remaining = max(frame_total - frames_done, 0)
    payload: dict[str, object] = {
        "step": "render_segments",
        "frame_index": frames_done,
        "frame_total": frame_total,
        "fps_observed": round(fps_observed, 2),
        "segment": segment_id,
    }
    if fps_observed > 0:
        payload["eta_s"] = round(remaining / fps_observed, 2)
    with contextlib.suppress(Exception):
        reporter.emit(ProgressEvent("frame", payload=payload))


def _abort_pool(pool: ProcessPoolExecutor, futures: dict) -> None:
    for future in futures:
        future.cancel()
    # Python 3.9+: terminate still-running workers immediately.
    with contextlib.suppress(TypeError):
        pool.shutdown(wait=False, cancel_futures=True)


def _cleanup(paths: list[Path]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


class _CancelledError(RuntimeError):
    pass
