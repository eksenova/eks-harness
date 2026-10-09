"""Face-presence marker extractor.

Walks the source video frame by frame, runs OpenCV's bundled Haar
cascade for frontal faces against each frame, and emits the start time
of every contiguous run of frames containing at least one face larger
than ``min_size`` pixels. The result is a "face appeared" event stream
rather than a per-frame presence stream.

Backend: OpenCV (``cv2``) only - the Haar cascade ships with the
``opencv-python`` wheel, so no extra model weights are needed. If
``cv2`` is not installed, the extractor raises ``ImportError`` with an
install hint; face markers are typically requested deliberately, so
silent fallback would be misleading.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.markers import FaceMarkers
from eks_harness.video.plugins.base import MarkerExtractor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["FaceMarkersExtractor"]

_LOG = logging.getLogger(__name__)


class FaceMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``FaceMarkers`` IR sources."""

    name: ClassVar[str] = "face_markers"
    handles: ClassVar[str] = "face_markers"

    def extract(self, source: FaceMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        media_path = _resolve_media_path(source.source, ctx)
        if media_path is None or not media_path.exists():
            _LOG.warning(
                "face_markers: cannot resolve media for source=%r; emitting empty stream",
                source.source,
            )
            return _empty_marker_set(source)

        # TODO: cache face detections keyed on (file digest, min_size). Skipped
        # for now because hashing large video files on every run is more
        # expensive than the detection in many cases.
        try:
            starts = _detect_face_runs(media_path, source.min_size)
        except _CV2Unavailable as exc:
            raise ImportError(
                "face_markers requires OpenCV; install via `pip install opencv-python`"
            ) from exc

        return _starts_to_marker_set(source, starts)


class _CV2Unavailable(RuntimeError):
    pass


def _detect_face_runs(media_path: Path, min_size: int) -> list[float]:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _CV2Unavailable from exc

    cascade_path = _haar_cascade_path(cv2)
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        _LOG.warning("face_markers: Haar cascade failed to load from %s", cascade_path)
        return []

    capture = cv2.VideoCapture(str(media_path))
    if not capture.isOpened():
        _LOG.warning("face_markers: cannot open %s; emitting empty stream", media_path)
        return []

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0.0:
        fps = 30.0

    starts: list[float] = []
    face_present_prev = False
    frame_idx = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=4,
                minSize=(int(min_size), int(min_size)),
            )
            face_present = len(faces) > 0
            if face_present and not face_present_prev:
                starts.append(frame_idx / fps)
            face_present_prev = face_present
            frame_idx += 1
    finally:
        capture.release()

    return sorted({round(t, 9) for t in starts})


def _haar_cascade_path(cv2_module: Any) -> str:
    data_dir = getattr(cv2_module, "data", None)
    if data_dir is None:
        return "haarcascade_frontalface_default.xml"
    return str(Path(data_dir.haarcascades) / "haarcascade_frontalface_default.xml")


def _resolve_media_path(spec: str, ctx: RenderContext | None) -> Path | None:
    candidate = Path(spec)
    if candidate.exists():
        return candidate
    if ctx is not None:
        ws_candidate = ctx.workspace / spec
        if ws_candidate.exists():
            return ws_candidate
    return None


def _empty_marker_set(source: FaceMarkers) -> MarkerSet:
    out = MarkerSet()
    out.streams[source.name] = []
    out.named[source.name] = []
    return out


def _starts_to_marker_set(source: FaceMarkers, starts: list[float]) -> MarkerSet:
    out = MarkerSet()
    sorted_starts = sorted(float(t) for t in starts)
    out.streams[source.name] = sorted_starts
    out.named[source.name] = list(sorted_starts)
    return out
