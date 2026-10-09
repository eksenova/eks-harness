"""Speaker-aware punch-in zoom.

Animates a center-anchored zoom around a per-frame focal point, optionally
triggered by speech events. When ``track_speaker`` is true and no explicit
``trigger`` is supplied, we look up sentence / word marker streams from the
active :class:`MarkerSet` to derive trigger times. The focal point itself is
the centroid of the largest detected face (MediaPipe), or the image center
when MediaPipe / faces are unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.compile.animated_resolve import resolve_animated
from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.effects import PunchIn
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.render.context import RenderContext

__all__ = ["PunchInPlugin"]


class _PunchInProcessor(FrameProcessor):
    def __init__(
        self,
        zoom_per_frame: np.ndarray | float,
        trigger_frames: list[int] | None,
        face_locator: Any | None,
    ) -> None:
        self._zoom = zoom_per_frame
        self._triggers = trigger_frames
        self._face_locator = face_locator

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        zoom = max(1.0, _sample(self._zoom, frame_idx))
        if self._triggers is not None and not _is_inside_trigger(self._triggers, frame_idx):
            zoom = 1.0
        if zoom <= 1.001:
            return frame
        height, width = frame.shape[:2]
        if self._face_locator is not None:
            cx, cy = self._face_locator.locate(frame)
        else:
            cx, cy = width / 2.0, height / 2.0
        crop_w = max(1, round(width / zoom))
        crop_h = max(1, round(height / zoom))
        x0 = max(0, min(width - crop_w, round(cx - crop_w / 2)))
        y0 = max(0, min(height - crop_h, round(cy - crop_h / 2)))
        cropped = frame[y0 : y0 + crop_h, x0 : x0 + crop_w]
        return _resize_nearest(cropped, width, height)


class PunchInPlugin(Effect):
    name: ClassVar[str] = "punch_in"
    model: ClassVar[type] = PunchIn
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: PunchIn, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        seg_duration = _segment_duration(ctx)
        zoom_resolved = resolve_animated(ir.zoom, ctx.project, ctx.markers, duration_seconds=seg_duration)
        zoom_value: np.ndarray | float = (
            zoom_resolved if isinstance(zoom_resolved, np.ndarray) else float(zoom_resolved)
        )

        triggers = _resolve_triggers(ir, ctx, ctx.project.fps, seg_duration)

        face_locator: Any | None = None
        try:
            from .auto_reframe import _FaceTracker

            face_locator = _FaceTracker()
        except RuntimeError:
            face_locator = None

        return _PunchInProcessor(zoom_value, triggers, face_locator)


def _resolve_triggers(
    ir: PunchIn, ctx: RenderContext, fps: float, seg_duration: float
) -> list[int] | None:
    if ir.trigger is not None:
        t = float(resolve_time(ir.trigger, ctx.project, ctx.markers))
        return [round(t * fps)]
    if not ir.track_speaker:
        return None
    streams: MarkerSet = ctx.markers
    times: list[float] = []
    for stream_name in ("sentence", "word"):
        if stream_name in streams.streams:
            times = list(streams.streams[stream_name])
            break
    if not times:
        return None
    n_frames = max(1, round(seg_duration * fps))
    return [min(n_frames - 1, max(0, round(t * fps))) for t in times]


def _is_inside_trigger(trigger_frames: list[int], frame_idx: int, window: int = 30) -> bool:
    return any(trigger <= frame_idx <= trigger + window for trigger in trigger_frames)


def _sample(value: np.ndarray | float, frame_idx: int) -> float:
    if isinstance(value, np.ndarray):
        idx = max(0, min(frame_idx, len(value) - 1))
        return float(value[idx])
    return float(value)


def _segment_duration(ctx: RenderContext) -> float:
    duration = ctx.extra.get("segment_duration_seconds")
    if isinstance(duration, (int, float)):
        return float(duration)
    return float(ctx.project.duration)


def _resize_nearest(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w == target_w and h == target_h:
        return frame
    y_idx = (np.arange(target_h) * h / target_h).astype(np.int64)
    x_idx = (np.arange(target_w) * w / target_w).astype(np.int64)
    out: np.ndarray = frame[y_idx[:, None], x_idx[None, :]]
    return out
