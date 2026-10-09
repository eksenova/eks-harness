"""Tests for :class:`EnergyMarkersExtractor`.

A fake ``librosa`` module supplies a controlled RMS curve so the
percentile gate and run-collapsing behavior can be asserted without
real audio.
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
from eks_harness.video.ir.markers import EnergyMarkers, MarkerSource
from eks_harness.video.plugins.builtin.markers.energy_markers import EnergyMarkersExtractor
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


def test_energy_markers_ir_round_trip() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(MarkerSource)
    parsed = adapter.validate_python(
        {
            "kind": "energy_markers",
            "name": "hype",
            "source": "audio_tracks[0]",
            "band": "high",
            "percentile": 0.9,
        }
    )
    assert isinstance(parsed, EnergyMarkers)
    assert parsed.band == "high"
    assert parsed.percentile == 0.9


def test_energy_markers_collapses_runs_into_starts(tmp_path: Path) -> None:
    audio = _stub_audio(tmp_path / "track.wav")
    source = EnergyMarkers(
        name="hype", source=str(audio), band="full", percentile=0.6
    )

    sr = 22050
    hop = 512
    # Two separated bursts well above the 60th percentile; the gate
    # should fire at each burst's first frame only (runs collapse).
    rms = np.array(
        [0.0, 0.0, 0.0, 0.9, 0.9, 0.9, 0.0, 0.0, 0.0, 0.8, 0.8, 0.0, 0.0],
        dtype=np.float32,
    )

    fake_librosa = types.SimpleNamespace(
        load=lambda *_args, **_kwargs: (np.zeros(sr * 4, dtype=np.float32), sr),
        feature=types.SimpleNamespace(rms=lambda **_kwargs: np.array([rms])),
        frames_to_time=lambda frames, sr=sr, hop_length=hop: np.asarray(frames, dtype=float) * hop / sr,
    )

    with patch.dict(sys.modules, {"librosa": fake_librosa}):
        result = EnergyMarkersExtractor().extract(source, _ctx(tmp_path))

    expected = sorted({round(3 * hop / sr, 9), round(9 * hop / sr, 9)})
    assert result.streams[source.name] == expected
    assert result.named[source.name] == expected


def test_energy_markers_emits_empty_when_librosa_missing(tmp_path: Path) -> None:
    audio = _stub_audio(tmp_path / "track.wav")
    source = EnergyMarkers(name="hype", source=str(audio))

    with patch.dict(sys.modules, {"librosa": None}):
        result = EnergyMarkersExtractor().extract(source, _ctx(tmp_path))

    assert result.streams[source.name] == []
