"""Helpers consumed by the MCP probe (see Phase 3)."""

from __future__ import annotations

from pathlib import Path

from eks_harness.video.compile.markers import (
    AudioMarkerReport,
    SpeechReport,
    extract_for_source,
    extract_speech_for_source,
)


def test_extract_for_source_missing_file_returns_empty(tmp_path: Path) -> None:
    report = extract_for_source(tmp_path / "no-such-file.wav")
    assert isinstance(report, AudioMarkerReport)
    assert report.bpm is None
    assert report.beat_times == []


def test_extract_speech_for_source_missing_file_returns_empty(tmp_path: Path) -> None:
    report = extract_speech_for_source(tmp_path / "missing.wav")
    assert isinstance(report, SpeechReport)
    assert report.words == []


def test_extract_speech_handles_unknown_backend(tmp_path: Path) -> None:
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"\x00" * 44)  # not a real wav, but the backend will refuse
    report = extract_speech_for_source(audio, backend="not-a-real-backend")
    assert report.words == []
