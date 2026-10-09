"""MomentumMarkers: drop/build/break detection on a synthetic arrangement."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("librosa")

from eks_harness.video import MomentumMarkers
from eks_harness.video.plugins.builtin.markers.momentum_markers import (
    MomentumMarkersExtractor,
    analyze_momentum,
)

SR = 22050


def _kick(n: int) -> np.ndarray:
    t = np.arange(n) / SR
    return np.sin(2 * np.pi * (55 + 90 * np.exp(-t * 40)) * t) * np.exp(-t * 9)


def _arrangement(path: Path) -> Path:
    bpm = 120
    beat = int(SR * 60 / bpm)
    rng = np.random.default_rng(0)
    quiet = 0.02 * rng.standard_normal(SR * 8)
    from scipy.signal import butter, sosfilt

    riser_t = np.arange(SR * 4) / SR
    hiss = sosfilt(butter(4, 2000, btype="high", fs=SR, output="sos"), rng.standard_normal(riser_t.size))
    riser = (riser_t / 4) ** 2 * 0.12 * hiss
    loud = np.zeros(SR * 10)
    for i in range(0, loud.size - beat, beat):
        loud[i:i + beat] += 0.9 * _kick(beat)
    loud += 0.15 * rng.standard_normal(loud.size)
    tail = 0.02 * rng.standard_normal(SR * 6)
    y = np.concatenate([quiet, riser, loud, tail]).astype(np.float32)
    sf.write(path, y / np.abs(y).max() * 0.9, SR)
    return path


def test_drop_lands_on_the_arrival_of_the_kicks(tmp_path: Path) -> None:
    audio = _arrangement(tmp_path / "song.wav")
    result = analyze_momentum(audio, MomentumMarkers(name="m", source=str(audio)))
    drops = [e["t"] for e in result["events"]["drop"]]
    assert any(abs(t - 12.0) < 0.4 for t in drops), drops
    builds = [e["t"] for e in result["events"]["build"]]
    assert any(6.0 <= t <= 11.0 for t in builds), builds
    breaks = [e["t"] for e in result["events"]["break"]]
    assert any(abs(t - 22.0) < 0.8 for t in breaks), breaks
    curve = result["curve"]["momentum"]
    assert min(curve) >= 0.0 and max(curve) <= 1.0


def test_extractor_publishes_namespaced_streams_and_writes_the_curve(tmp_path: Path) -> None:
    from eks_harness.video.compile.markers import _MinimalMarkerCtx
    from eks_harness.video.ir import Project

    audio = _arrangement(tmp_path / "song.wav")
    source = MomentumMarkers(name="song", source=str(audio), curve_out="momentum.json")
    ctx = _MinimalMarkerCtx(project=Project(fps=30, resolution=(64, 64), duration=1.0), workspace=tmp_path)
    markers = MomentumMarkersExtractor().extract(source, ctx)  # type: ignore[arg-type]
    for event in ("drop", "build", "break", "section", "peak"):
        assert f"song.{event}" in markers.streams
        assert f"song.{event}" in markers.named
    assert markers.streams["song.drop"]
    payload = json.loads((tmp_path / "momentum.json").read_text())
    assert payload["events"]["drop"] and payload["curve"]["t"]
