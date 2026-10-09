"""Auto-reframe to a target aspect ratio.

Picks a per-frame crop centered on whatever ``tracker`` reports as
"interesting" (faces by default, edge-density centroid as a fallback) and
EMA-smooths the centre to avoid jitter. The output is a center-crop of the
input scaled so that the target aspect fills the frame.

For ``tracker="face"`` we use MediaPipe's Face Detector when available; on
hosts without MediaPipe the plugin raises a clear install error at
``open`` time. ``tracker="salience"`` uses ``cv2``'s Canny edge detector
when available, again raising a clear install error otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import AutoReframe
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["AutoReframePlugin"]


class _AutoReframeProcessor(FrameProcessor):
    parallel_safe: ClassVar[bool] = False

    def __init__(
        self,
        target_aspect: tuple[int, int],
        tracker: Any,
        smoothing: float,
        output_size: tuple[int, int],
    ) -> None:
        self._target_w, self._target_h = output_size
        self._target_aspect = target_aspect[0] / target_aspect[1]
        self._tracker = tracker
        self._smoothing = max(0.0, min(1.0, smoothing))
        self._cx_smoothed: float | None = None
        self._cy_smoothed: float | None = None

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        height, width = frame.shape[:2]
        cx, cy = self._tracker.locate(frame)
        if self._cx_smoothed is None:
            self._cx_smoothed, self._cy_smoothed = cx, cy
        else:
            self._cx_smoothed = self._smoothing * self._cx_smoothed + (1.0 - self._smoothing) * cx
            assert self._cy_smoothed is not None
            self._cy_smoothed = self._smoothing * self._cy_smoothed + (1.0 - self._smoothing) * cy

        crop_h = height
        crop_w = round(crop_h * self._target_aspect)
        if crop_w > width:
            crop_w = width
            crop_h = round(crop_w / self._target_aspect)
        x0 = round(self._cx_smoothed - crop_w / 2)
        y0 = round((self._cy_smoothed or cy) - crop_h / 2)
        x0 = max(0, min(width - crop_w, x0))
        y0 = max(0, min(height - crop_h, y0))
        cropped = frame[y0 : y0 + crop_h, x0 : x0 + crop_w]
        return _resize_nearest(cropped, self._target_w, self._target_h)


class AutoReframePlugin(Effect):
    name: ClassVar[str] = "auto_reframe"
    model: ClassVar[type] = AutoReframe
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: AutoReframe, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        tracker: _FaceTracker | _SalienceTracker = (
            _FaceTracker() if ir.tracker == "face" else _SalienceTracker()
        )
        target_w, target_h = ctx.project.resolution
        return _AutoReframeProcessor(
            ir.target_aspect, tracker, ir.smoothing, (target_w, target_h)
        )


_BLAZEFACE_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/latest/blaze_face_short_range.tflite"
)


def _blazeface_model_path() -> str:
    """Resolve (and download once) the BlazeFace short-range tflite."""

    from pathlib import Path
    import urllib.request

    cache_dir = Path.home() / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "mp_face_detector.tflite"
    if not target.exists() or target.stat().st_size < 1024:
        urllib.request.urlretrieve(_BLAZEFACE_URL, target)
    return str(target)


class _FaceTracker:
    """Face localizer backed by MediaPipe's modern ``tasks`` Vision API.

    The legacy ``mp.solutions.face_detection`` namespace was removed in
    MediaPipe 0.10.30+; this implementation uses
    :class:`mediapipe.tasks.python.vision.FaceDetector` with the
    BlazeFace-short-range tflite model (cached at
    ``~/.cache/mp_face_detector.tflite`` after first download).
    """

    def __init__(self) -> None:
        try:
            import mediapipe as mp  # type: ignore[import-not-found]
            from mediapipe.tasks import python as mp_python  # type: ignore[import-not-found]
            from mediapipe.tasks.python import vision as mp_vision  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "auto_reframe(tracker='face') requires mediapipe; "
                "install with `pip install eks-harness[ml]`"
            ) from exc

        self._mp = mp
        model_path = _blazeface_model_path()
        options = mp_vision.FaceDetectorOptions(
            base_options=mp_python.BaseOptions(model_asset_path=model_path),
            min_detection_confidence=0.5,
        )
        self._detector = mp_vision.FaceDetector.create_from_options(options)

    def locate(self, frame: Any) -> tuple[float, float]:
        height, width = frame.shape[:2]
        # mediapipe Image expects RGB; our frames are BGR.
        rgb = np.ascontiguousarray(frame[..., ::-1])
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._detector.detect(mp_image)
        detections = getattr(result, "detections", None) or []
        if not detections:
            return width / 2.0, height / 2.0
        cx_total = 0.0
        cy_total = 0.0
        weight_total = 0.0
        for det in detections:
            bbox = det.bounding_box  # origin_x, origin_y, width, height in pixels
            score = float(det.categories[0].score) if det.categories else 1.0
            cx = bbox.origin_x + bbox.width / 2.0
            cy = bbox.origin_y + bbox.height / 2.0
            cx_total += cx * score
            cy_total += cy * score
            weight_total += score
        if weight_total == 0.0:
            return width / 2.0, height / 2.0
        return cx_total / weight_total, cy_total / weight_total


class _SalienceTracker:
    def __init__(self) -> None:
        self._cv2 = _import_cv2_or_raise()

    def locate(self, frame: Any) -> tuple[float, float]:
        gray = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2GRAY)
        edges = self._cv2.Canny(gray, 100, 200)
        n_labels, _, stats, centroids = self._cv2.connectedComponentsWithStats(edges, 8)
        if n_labels <= 1:
            height, width = frame.shape[:2]
            return width / 2.0, height / 2.0
        areas = stats[1:, self._cv2.CC_STAT_AREA]
        biggest = int(np.argmax(areas)) + 1
        cx, cy = centroids[biggest]
        return float(cx), float(cy)


def _import_cv2_or_raise() -> Any:
    try:
        import cv2  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError as exc:
        raise RuntimeError(
            "auto_reframe(tracker='salience') requires opencv-python-headless; "
            "install with `pip install eks-harness[ml]`"
        ) from exc
    return cv2


def _resize_nearest(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w == target_w and h == target_h:
        return frame
    y_idx = (np.arange(target_h) * h / target_h).astype(np.int64)
    x_idx = (np.arange(target_w) * w / target_w).astype(np.int64)
    out: np.ndarray = frame[y_idx[:, None], x_idx[None, :]]
    return out
