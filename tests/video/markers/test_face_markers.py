"""Tests for :class:`FaceMarkersExtractor`.

A fake ``cv2`` module emulates :class:`VideoCapture` and
:class:`CascadeClassifier`, feeding a synthetic frame stream so the
contiguous-run detection logic can be asserted without OpenCV.
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
from eks_harness.video.ir.markers import FaceMarkers, MarkerSource
from eks_harness.video.plugins.builtin.markers.face_markers import FaceMarkersExtractor
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


class _FakeCascade:
    def __init__(self, face_per_frame: list[bool]) -> None:
        self._face_per_frame = face_per_frame
        self._idx = 0

    def empty(self) -> bool:
        return False

    def detectMultiScale(self, _gray: Any, **_kwargs: Any) -> list[tuple[int, int, int, int]]:
        has_face = self._face_per_frame[self._idx] if self._idx < len(self._face_per_frame) else False
        self._idx += 1
        return [(0, 0, 100, 100)] if has_face else []


def _build_fake_cv2(face_per_frame: list[bool], fps: float = 10.0) -> types.SimpleNamespace:
    frames = [np.zeros((4, 4, 3), dtype=np.uint8) for _ in face_per_frame]
    return types.SimpleNamespace(
        CAP_PROP_FPS=5,
        COLOR_BGR2GRAY=6,
        data=types.SimpleNamespace(haarcascades="/fake/cascades/"),
        VideoCapture=lambda _path: _FakeCapture(frames, fps),
        CascadeClassifier=lambda _path: _FakeCascade(face_per_frame),
        cvtColor=lambda frame, _code: frame[..., 0],
    )


def test_face_markers_ir_round_trip() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(MarkerSource)
    parsed = adapter.validate_python(
        {
            "kind": "face_markers",
            "name": "faces",
            "source": "talking_head.mp4",
            "min_size": 100,
        }
    )
    assert isinstance(parsed, FaceMarkers)
    assert parsed.min_size == 100


def test_face_markers_emits_one_marker_per_face_run(tmp_path: Path) -> None:
    video = _stub_video(tmp_path / "talking.mp4")
    source = FaceMarkers(name="faces", source=str(video))

    # Two runs of face presence: frames 1-3 (starts at frame 1) and frames 6-7.
    face_per_frame = [False, True, True, True, False, False, True, True, False]
    fake_cv2 = _build_fake_cv2(face_per_frame, fps=10.0)

    with patch.dict(sys.modules, {"cv2": fake_cv2}):
        result = FaceMarkersExtractor().extract(source, _ctx(tmp_path))

    assert result.streams[source.name] == [0.1, 0.6]
    assert result.named[source.name] == [0.1, 0.6]


def test_face_markers_raises_when_cv2_missing(tmp_path: Path) -> None:
    video = _stub_video(tmp_path / "talking.mp4")
    source = FaceMarkers(name="faces", source=str(video))

    with patch.dict(sys.modules, {"cv2": None}), pytest.raises(ImportError, match="opencv-python"):
        FaceMarkersExtractor().extract(source, _ctx(tmp_path))
