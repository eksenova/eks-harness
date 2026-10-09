"""Tests for :class:`SilenceMarkersExtractor`.

Mocks ``librosa`` at the import site inside ``silence_markers._detect_silences``
so tests run without ``librosa`` installed and without producing a real
audio file.
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
from eks_harness.video.ir.markers import MarkerSource, SilenceMarkers
from eks_harness.video.plugins.builtin.markers.silence_markers import SilenceMarkersExtractor
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


def test_silence_markers_ir_round_trip() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(MarkerSource)
    parsed = adapter.validate_python(
        {
            "kind": "silence_markers",
            "name": "silences",
            "source": "audio_tracks[0]",
            "threshold_db": -45.0,
            "min_duration": 0.5,
        }
    )
    assert isinstance(parsed, SilenceMarkers)
    assert parsed.threshold_db == -45.0
    assert parsed.min_duration == 0.5


def test_silence_markers_detects_synthetic_silent_region(tmp_path: Path) -> None:
    audio = _stub_audio(tmp_path / "track.wav")
    source = SilenceMarkers(name="silences", source=str(audio), min_duration=0.2)

    # Build a fake librosa module that returns a known RMS curve:
    # 4 seconds total at sr=22050, hop=512 -> ~172 frames. We put a
    # 1.0s silent dip starting at frame 50.
    sr = 22050
    hop = 512
    n_frames = 172
    rms = np.full(n_frames, 0.5, dtype=np.float32)
    silent_start = 50
    silent_end = silent_start + int(1.0 * sr / hop)
    rms[silent_start:silent_end] = 1e-6

    fake_librosa = types.SimpleNamespace(
        load=lambda *_args, **_kwargs: (np.zeros(sr * 4, dtype=np.float32), sr),
        feature=types.SimpleNamespace(rms=lambda **_kwargs: np.array([rms])),
        amplitude_to_db=lambda x, ref=1.0: 20.0 * np.log10(np.maximum(x, 1e-10)),
        frames_to_time=lambda frames, sr=sr, hop_length=hop: frames * hop / sr,
    )

    with patch.dict(sys.modules, {"librosa": fake_librosa}):
        result = SilenceMarkersExtractor().extract(source, _ctx(tmp_path))

    expected_start = silent_start * hop / sr
    assert source.name in result.streams
    assert len(result.streams[source.name]) == 1
    assert abs(result.streams[source.name][0] - expected_start) < 0.05
    assert result.named[source.name] == result.streams[source.name]


def test_silence_markers_emits_empty_when_librosa_missing(tmp_path: Path) -> None:
    audio = _stub_audio(tmp_path / "track.wav")
    source = SilenceMarkers(name="silences", source=str(audio))

    # Force the import inside `_detect_silences` to fail without removing
    # a possibly-real `librosa` from the environment.
    with patch.dict(sys.modules, {"librosa": None}):
        result = SilenceMarkersExtractor().extract(source, _ctx(tmp_path))

    assert result.streams[source.name] == []
    assert result.named[source.name] == []
