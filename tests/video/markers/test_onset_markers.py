"""Tests for :class:`OnsetMarkersExtractor`.

The librosa fallback path is exercised by injecting a fake ``librosa``
module into ``sys.modules`` that returns known onset frames. The madmom
path is patched to be unavailable so the chain falls through to librosa
deterministically.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
from pydantic import TypeAdapter
from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.video.ir.markers import MarkerSource, OnsetMarkers
from eks_harness.video.plugins.builtin.markers import onset_markers as onset_module
from eks_harness.video.plugins.builtin.markers.onset_markers import OnsetMarkersExtractor
from eks_harness.video.render.context import RenderContext, RenderOptions


def _stub_audio(path: Path) -> Path:
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
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


def test_onset_markers_ir_round_trip() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(MarkerSource)
    parsed = adapter.validate_python(
        {
            "kind": "onset_markers",
            "name": "onsets",
            "source": "audio_tracks[0]",
            "sensitivity": 0.7,
        }
    )
    assert isinstance(parsed, OnsetMarkers)
    assert parsed.sensitivity == 0.7


def test_onset_markers_falls_through_to_librosa(tmp_path: Path) -> None:
    audio = _stub_audio(tmp_path / "track.wav")
    source = OnsetMarkers(name="onsets", source=str(audio))

    sr = 22050
    onset_frames = np.array([10, 80, 150], dtype=np.int64)

    fake_librosa = types.SimpleNamespace(
        load=lambda *_args, **_kwargs: (np.zeros(sr * 4, dtype=np.float32), sr),
        onset=types.SimpleNamespace(onset_detect=lambda **_kwargs: onset_frames),
        frames_to_time=lambda frames, sr=sr: np.asarray(frames, dtype=float) * 512.0 / sr,
    )

    def unavailable(*_args: Any, **_kwargs: Any) -> list[float]:
        raise onset_module._BackendUnavailable("madmom not installed")

    with (
        patch.object(onset_module, "_run_madmom", unavailable),
        patch.dict(sys.modules, {"librosa": fake_librosa}),
    ):
        result = OnsetMarkersExtractor().extract(source, _ctx(tmp_path))

    expected = sorted(float(f) * 512.0 / sr for f in onset_frames)
    assert result.streams[source.name] == expected
    assert result.named[source.name] == expected


def test_onset_markers_emits_empty_when_no_backend(tmp_path: Path) -> None:
    audio = _stub_audio(tmp_path / "track.wav")
    source = OnsetMarkers(name="onsets", source=str(audio))

    def unavailable_backend(*_args: Any, **_kwargs: Any) -> list[float]:
        raise onset_module._BackendUnavailable("nope")

    with (
        patch.object(onset_module, "_run_madmom", unavailable_backend),
        patch.object(onset_module, "_run_librosa", unavailable_backend),
    ):
        result = OnsetMarkersExtractor().extract(source, _ctx(tmp_path))

    assert result.streams[source.name] == []
