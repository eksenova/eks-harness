"""Tests for :class:`MotionMarkersExtractor`.

Patches ``cv2`` with a fake :class:`VideoCapture` that yields a
controlled frame sequence so the inter-frame diff thresholding can be
asserted without OpenCV.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from pydantic import TypeAdapter
from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.video.ir.markers import MarkerSource, MotionMarkers
from eks_harness.video.plugins.builtin.markers.motion_markers import MotionMarkersExtractor
from eks_harness.video.render.context import RenderContext, RenderOptions


def _stub_video(path: Path) -> Path:
    path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
    return path


def _ctx(tmp_path: Path) -> RenderContext:
    project = Project(
        fps=30,
        resolution=(64, 64),
        duration=4.0,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="seg",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="x.png"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=4.0),
                    )
                ],
            )
        ],
    )
    return RenderContext(
        project=project,
        options=RenderOptions(output=tmp_path / "out.mp4"),
        markers=MarkerSet(),
        workspace=tmp_path,
        cache_dir=tmp_path / "cache",
    )


class _FakeCapture:
    def __init__(self, frames: list[np.ndarray], fps: float) -> None:
        self._frames = frames
        self._fps = fps
        self._idx = 0
        self._open = True

    def isOpened(self) -> bool:
        return self._open

    def get(self, _prop: int) -> float:
        return self._fps

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self._idx >= len(self._frames):
            return False, None
        frame = self._frames[self._idx]
        self._idx += 1
        return True, frame

    def release(self) -> None:
        self._open = False


def _build_fake_cv2(frames: list[np.ndarray], fps: float = 10.0) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        CAP_PROP_FPS=5,
        COLOR_BGR2GRAY=6,
        VideoCapture=lambda _path: _FakeCapture(frames, fps),
        cvtColor=lambda frame, _code: frame[..., 0].astype(np.int16),
        absdiff=lambda a, b: np.abs(a.astype(np.int16) - b.astype(np.int16)).astype(np.uint8),
    )


def test_motion_markers_ir_round_trip() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(MarkerSource)
    parsed = adapter.validate_python(
        {
            "kind": "motion_markers",
            "name": "motion",
            "source": "clip.mp4",
            "threshold": 25.0,
        }
    )
    assert isinstance(parsed, MotionMarkers)
    assert parsed.threshold == 25.0


def test_motion_markers_flags_high_diff_frames(tmp_path: Path) -> None:
    video = _stub_video(tmp_path / "clip.mp4")
    source = MotionMarkers(name="motion", source=str(video), threshold=30.0)

    # 5 frames at 10 fps. Frame 2 jumps from black to white (mean diff = 255),
    # frame 3 stays white (diff = 0), frame 4 jumps back to black (diff = 255).
    black = np.zeros((4, 4, 3), dtype=np.uint8)
    white = np.full((4, 4, 3), 255, dtype=np.uint8)
    frames = [black, black, white, white, black]

    fake_cv2 = _build_fake_cv2(frames, fps=10.0)
    with patch.dict(sys.modules, {"cv2": fake_cv2}):
        result = MotionMarkersExtractor().extract(source, _ctx(tmp_path))

    assert result.streams[source.name] == [0.2, 0.4]
    assert result.named[source.name] == [0.2, 0.4]


def test_motion_markers_raises_when_cv2_missing(tmp_path: Path) -> None:
    video = _stub_video(tmp_path / "clip.mp4")
    source = MotionMarkers(name="motion", source=str(video))

    with patch.dict(sys.modules, {"cv2": None}), pytest.raises(ImportError, match="opencv-python"):
        MotionMarkersExtractor().extract(source, _ctx(tmp_path))
