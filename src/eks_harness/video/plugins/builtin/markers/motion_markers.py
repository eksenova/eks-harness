"""High-motion marker extractor.

Walks the source video frame by frame and computes the mean absolute
difference between each frame and the previous one (after grayscale
conversion). Frames whose mean diff exceeds ``threshold`` produce a
marker at the frame's timestamp.

Backend: OpenCV (``cv2``) only. If ``cv2`` is not installed the
extractor raises ``ImportError`` with an install hint - same contract
as :class:`FaceMarkersExtractor`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.markers import MotionMarkers
from eks_harness.video.plugins.base import MarkerExtractor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["MotionMarkersExtractor"]

_LOG = logging.getLogger(__name__)


class MotionMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``MotionMarkers`` IR sources."""

    name: ClassVar[str] = "motion_markers"
    handles: ClassVar[str] = "motion_markers"

    def extract(self, source: MotionMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        media_path = _resolve_media_path(source.source, ctx)
        if media_path is None or not media_path.exists():
            _LOG.warning(
                "motion_markers: cannot resolve media for source=%r; emitting empty stream",
                source.source,
            )
            return _empty_marker_set(source)

        # TODO: cache motion detections keyed on (file digest, threshold).
        try:
            hits = _detect_motion_frames(media_path, source.threshold)
        except _CV2Unavailable as exc:
            raise ImportError(
                "motion_markers requires OpenCV; install via `pip install opencv-python`"
            ) from exc

        return _hits_to_marker_set(source, hits)


class _CV2Unavailable(RuntimeError):
    pass


def _detect_motion_frames(media_path: Path, threshold: float) -> list[float]:
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np
    except ImportError as exc:
        raise _CV2Unavailable from exc

    capture = cv2.VideoCapture(str(media_path))
    if not capture.isOpened():
        _LOG.warning("motion_markers: cannot open %s; emitting empty stream", media_path)
        return []

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0.0:
        fps = 30.0

    prev_gray = None
    hits: list[float] = []
    frame_idx = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                mean_diff = float(np.mean(diff))
                if mean_diff >= float(threshold):
                    hits.append(frame_idx / fps)
            prev_gray = gray
            frame_idx += 1
    finally:
        capture.release()

    return sorted({round(t, 9) for t in hits})


def _resolve_media_path(spec: str, ctx: RenderContext | None) -> Path | None:
    candidate = Path(spec)
    if candidate.exists():
        return candidate
    if ctx is not None:
        ws_candidate = ctx.workspace / spec
        if ws_candidate.exists():
            return ws_candidate
    return None


def _empty_marker_set(source: MotionMarkers) -> MarkerSet:
    out = MarkerSet()
    out.streams[source.name] = []
    out.named[source.name] = []
    return out


def _hits_to_marker_set(source: MotionMarkers, hits: list[float]) -> MarkerSet:
    out = MarkerSet()
    sorted_hits = sorted(float(t) for t in hits)
    out.streams[source.name] = sorted_hits
    out.named[source.name] = list(sorted_hits)
    return out
