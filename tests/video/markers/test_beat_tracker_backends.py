"""Backend-selection and BeatNet parsing tests for ``BeatTrackerExtractor``.

These tests fully mock every backend so they run without madmom, BeatNet
or librosa actually being importable. The real-backend smoke path is not
covered here - gated behind a manual script - because BeatNet downloads
torch weights.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.video.ir.markers import BeatTracker
from eks_harness.video.plugins.builtin.markers import beat_tracker as bt_module
from eks_harness.video.plugins.builtin.markers.beat_tracker import (
    BeatTrackerExtractor,
    _parse_beatnet_output,
)
from eks_harness.video.render.context import RenderContext, RenderOptions


def _silent_audio(path: Path) -> Path:
    """Create a placeholder file so ``_resolve_audio_path`` succeeds.

    Backends are mocked, so the bytes are never decoded.
    """

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


def test_parse_beatnet_output_separates_beats_and_downbeats() -> None:
    raw = np.array(
        [
            [0.50, 1],  # downbeat
            [1.00, 2],
            [1.50, 3],
            [2.00, 4],
            [2.50, 1],  # downbeat
            [3.00, 2],
        ],
        dtype=float,
    )
    beats, downbeats = _parse_beatnet_output(raw)
    assert beats == [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    assert downbeats == [0.5, 2.5]


def test_parse_beatnet_output_handles_empty_and_1d() -> None:
    assert _parse_beatnet_output(np.zeros((0, 2))) == ([], [])
    beats, downbeats = _parse_beatnet_output(np.array([0.7, 1.0]))
    assert beats == [0.7]
    assert downbeats == [0.7]


def test_chain_for_auto_returns_full_priority_order() -> None:
    ext = BeatTrackerExtractor()
    assert ext._chain_for("auto") == ("beatnet", "madmom", "librosa")


def test_chain_for_explicit_pin_puts_backend_first() -> None:
    ext = BeatTrackerExtractor()
    assert ext._chain_for("madmom")[0] == "madmom"
    assert set(ext._chain_for("madmom")) == {"beatnet", "madmom", "librosa"}


def test_beatnet_backend_runs_when_available(tmp_path: Path) -> None:
    audio = _silent_audio(tmp_path / "song.wav")
    source = BeatTracker(name="b", source=str(audio), backend="auto")

    beatnet_output = np.array(
        [[0.0, 1], [0.5, 2], [1.0, 3], [1.5, 4], [2.0, 1]], dtype=float
    )

    def fake_beatnet(_self: Any, _path: Path, src: BeatTracker) -> dict[str, list[float]]:
        beats, downbeats = _parse_beatnet_output(beatnet_output)
        payload = {
            "beat": beats,
            "downbeat": downbeats,
            "kick": [0.0, 1.0, 2.0],
            "snare": [0.5, 1.5],
        }
        return {s: payload[s] for s in src.streams}

    with patch.object(BeatTrackerExtractor, "_run_beatnet", fake_beatnet):
        result = BeatTrackerExtractor().extract(source, _ctx(tmp_path))

    assert result.streams["beat"] == [0.0, 0.5, 1.0, 1.5, 2.0]
    assert result.streams["downbeat"] == [0.0, 2.0]
    assert result.streams["kick"] == [0.0, 1.0, 2.0]
    assert result.streams["snare"] == [0.5, 1.5]


def test_auto_chain_falls_through_to_madmom_when_beatnet_unavailable(
    tmp_path: Path,
) -> None:
    audio = _silent_audio(tmp_path / "song.wav")
    source = BeatTracker(name="b", source=str(audio), backend="auto")

    def unavailable(_self: Any, _path: Path, _src: BeatTracker) -> dict[str, list[float]]:
        raise bt_module._BackendUnavailable("BeatNet not installed")

    def madmom_payload(_self: Any, _path: Path, src: BeatTracker) -> dict[str, list[float]]:
        payload = {"beat": [0.5], "downbeat": [0.5], "kick": [0.5], "snare": []}
        return {s: payload[s] for s in src.streams}

    with (
        patch.object(BeatTrackerExtractor, "_run_beatnet", unavailable),
        patch.object(BeatTrackerExtractor, "_run_madmom", madmom_payload),
    ):
        result = BeatTrackerExtractor().extract(source, _ctx(tmp_path))

    assert result.streams["beat"] == [0.5]


def test_explicit_pin_to_librosa_skips_beatnet_and_madmom(tmp_path: Path) -> None:
    audio = _silent_audio(tmp_path / "song.wav")
    source = BeatTracker(name="b", source=str(audio), backend="librosa")

    called: list[str] = []

    def record(name: str) -> Any:
        def fn(_self: Any, _path: Path, src: BeatTracker) -> dict[str, list[float]]:
            called.append(name)
            if name != "librosa":
                raise AssertionError(f"{name} should not be called when pinned to librosa")
            payload = {"beat": [1.0], "downbeat": [], "kick": [1.0], "snare": []}
            return {s: payload[s] for s in src.streams}

        return fn

    with (
        patch.object(BeatTrackerExtractor, "_run_beatnet", record("beatnet")),
        patch.object(BeatTrackerExtractor, "_run_madmom", record("madmom")),
        patch.object(BeatTrackerExtractor, "_run_librosa", record("librosa")),
    ):
        result = BeatTrackerExtractor().extract(source, _ctx(tmp_path))

    assert called == ["librosa"]
    assert result.streams["beat"] == [1.0]


def test_explicit_pin_falls_through_when_pinned_backend_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    audio = _silent_audio(tmp_path / "song.wav")
    source = BeatTracker(name="b", source=str(audio), backend="beatnet")

    def unavailable(_self: Any, _path: Path, _src: BeatTracker) -> dict[str, list[float]]:
        raise bt_module._BackendUnavailable("BeatNet not installed")

    def madmom_payload(_self: Any, _path: Path, src: BeatTracker) -> dict[str, list[float]]:
        payload = {"beat": [0.5], "downbeat": [], "kick": [0.5], "snare": []}
        return {s: payload[s] for s in src.streams}

    with (
        caplog.at_level("WARNING", logger=bt_module.__name__),
        patch.object(BeatTrackerExtractor, "_run_beatnet", unavailable),
        patch.object(BeatTrackerExtractor, "_run_madmom", madmom_payload),
    ):
        result = BeatTrackerExtractor().extract(source, _ctx(tmp_path))

    assert result.streams["beat"] == [0.5]
    assert any(
        "backend='beatnet' unavailable" in rec.message for rec in caplog.records
    )


def test_cache_key_includes_resolved_backend(tmp_path: Path) -> None:
    audio = _silent_audio(tmp_path / "song.wav")
    source = BeatTracker(name="b", source=str(audio), backend="auto")
    ctx = _ctx(tmp_path)

    def beatnet_payload(_self: Any, _path: Path, src: BeatTracker) -> dict[str, list[float]]:
        payload = {"beat": [0.25], "downbeat": [0.25], "kick": [], "snare": []}
        return {s: payload[s] for s in src.streams}

    with patch.object(BeatTrackerExtractor, "_run_beatnet", beatnet_payload):
        BeatTrackerExtractor().extract(source, ctx)

    cache_files = list((ctx.cache_dir / "markers").glob("*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].name.endswith("-beatnet.json")


def test_cache_hit_short_circuits_backend(tmp_path: Path) -> None:
    audio = _silent_audio(tmp_path / "song.wav")
    source = BeatTracker(name="b", source=str(audio), backend="beatnet")
    ctx = _ctx(tmp_path)

    call_count = {"n": 0}

    def beatnet_payload(_self: Any, _path: Path, src: BeatTracker) -> dict[str, list[float]]:
        call_count["n"] += 1
        payload = {"beat": [0.1, 0.6], "downbeat": [0.1], "kick": [], "snare": []}
        return {s: payload[s] for s in src.streams}

    with patch.object(BeatTrackerExtractor, "_run_beatnet", beatnet_payload):
        BeatTrackerExtractor().extract(source, ctx)
        BeatTrackerExtractor().extract(source, ctx)

    assert call_count["n"] == 1


def test_all_backends_unavailable_falls_back_to_stub(tmp_path: Path) -> None:
    audio = _silent_audio(tmp_path / "song.wav")
    source = BeatTracker(name="b", source=str(audio), backend="auto", bpm=120.0)

    def unavailable(_self: Any, _path: Path, _src: BeatTracker) -> dict[str, list[float]]:
        raise bt_module._BackendUnavailable("nope")

    with (
        patch.object(BeatTrackerExtractor, "_run_beatnet", unavailable),
        patch.object(BeatTrackerExtractor, "_run_madmom", unavailable),
        patch.object(BeatTrackerExtractor, "_run_librosa", unavailable),
    ):
        result = BeatTrackerExtractor().extract(source, _ctx(tmp_path))

    # Stub at 120 BPM over 4s duration produces beats every 0.5s.
    assert result.streams["beat"][:3] == [0.0, 0.5, 1.0]
