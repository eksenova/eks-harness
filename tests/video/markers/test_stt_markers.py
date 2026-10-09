"""STT marker extractor: end-to-end with a real backend if installed.

When the ``faster-whisper`` extra is missing the test is skipped; this is
the documented contract for the ``ml`` extras and matches the beat-tracker
test layout.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("faster_whisper")
pytest.importorskip("soundfile")

import soundfile as sf  # noqa: E402

from eks_harness.video.compile.markers import MarkerSet  # noqa: E402
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track  # noqa: E402
from eks_harness.video.ir.markers import STTMarkers  # noqa: E402
from eks_harness.video.plugins.builtin.markers.stt_markers import STTMarkersExtractor  # noqa: E402
from eks_harness.video.plugins.registry import load_builtins  # noqa: E402
from eks_harness.video.render.context import RenderContext, RenderOptions  # noqa: E402


def _silent_wav(path: Path, seconds: float = 2.0, sample_rate: int = 16000) -> Path:
    samples = np.zeros(int(seconds * sample_rate), dtype=np.float32)
    sf.write(str(path), samples, sample_rate)
    return path


def _ctx(tmp_path: Path) -> RenderContext:
    project = Project(
        fps=30,
        resolution=(64, 64),
        duration=2.0,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="seg",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="x.png"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=2.0),
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


def test_stt_markers_returns_word_stream_for_silent_audio(tmp_path: Path) -> None:
    load_builtins()

    audio = _silent_wav(tmp_path / "silence.wav")
    source = STTMarkers(name="vo", source=str(audio))
    extractor = STTMarkersExtractor()

    result = extractor.extract(source, _ctx(tmp_path))

    assert "word" in result.streams
    assert "sentence" in result.streams
    assert result.streams["word"] == sorted(result.streams["word"])
