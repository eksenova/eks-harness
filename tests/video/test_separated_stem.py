"""Round-trip tests for the SeparatedStem audio media kind.

The heavy Demucs inference is skipped unless ``demucs`` is installed; the
IR-level tests run in all environments.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from eks_harness.video import (
    AudioFile,
    AudioSegment,
    AudioTrack,
    Project,
    Seconds,
    SeparatedStem,
)
from eks_harness.video.ir.media import MediaSource


def test_separated_stem_round_trip() -> None:
    stem = SeparatedStem(
        source=AudioFile(path="song.mp3"),
        stems=["vocals"],
    )
    rebuilt = SeparatedStem.model_validate(stem.model_dump())
    assert rebuilt == stem
    assert rebuilt.model == "htdemucs"
    assert rebuilt.stems == ["vocals"]


def test_separated_stem_multi_stem_round_trip() -> None:
    stem = SeparatedStem(
        source=AudioFile(path="song.mp3"),
        stems=["vocals", "drums"],
        model="htdemucs_ft",
    )
    rebuilt = SeparatedStem.model_validate(stem.model_dump())
    assert rebuilt == stem
    assert rebuilt.model == "htdemucs_ft"


def test_separated_stem_empty_stems_rejected() -> None:
    with pytest.raises(ValidationError):
        SeparatedStem(source=AudioFile(path="song.mp3"), stems=[])


def test_separated_stem_unknown_stem_rejected() -> None:
    with pytest.raises(ValidationError):
        SeparatedStem(
            source=AudioFile(path="song.mp3"),
            stems=["vocals", "harp"],  # type: ignore[list-item]
        )


def test_separated_stem_unknown_model_rejected() -> None:
    with pytest.raises(ValidationError):
        SeparatedStem(
            source=AudioFile(path="song.mp3"),
            stems=["vocals"],
            model="my_custom_model",  # type: ignore[arg-type]
        )


def test_separated_stem_is_part_of_media_source_union() -> None:
    """The discriminated MediaSource union must accept SeparatedStem."""
    adapter = TypeAdapter(MediaSource)
    payload = {
        "kind": "separated_stem",
        "source": {"kind": "audio_file", "path": "song.mp3"},
        "stems": ["vocals"],
        "model": "htdemucs",
    }
    parsed = adapter.validate_python(payload)
    assert isinstance(parsed, SeparatedStem)


def test_audio_segment_with_separated_stem_media() -> None:
    seg = AudioSegment(
        id="vocals_only",
        start=Seconds(t=0),
        media=SeparatedStem(
            source=AudioFile(path="song.mp3"),
            stems=["vocals"],
        ),
        in_=Seconds(t=0),
        out=Seconds(t=10),
    )
    rebuilt = AudioSegment.model_validate(seg.model_dump(by_alias=True))
    assert isinstance(rebuilt.media, SeparatedStem)
    assert rebuilt.media.stems == ["vocals"]


def test_project_with_separated_stem_round_trip() -> None:
    project = Project(
        fps=30,
        resolution=(1080, 1920),
        duration=10.0,
        tracks=[],
        audio_tracks=[
            AudioTrack(
                name="music",
                segments=[
                    AudioSegment(
                        id="vocals",
                        start=Seconds(t=0),
                        media=SeparatedStem(
                            source=AudioFile(path="song.mp3"),
                            stems=["vocals", "other"],
                        ),
                        in_=Seconds(t=0),
                        out=Seconds(t=5),
                    )
                ],
            )
        ],
    )
    rebuilt = Project.model_validate(project.model_dump(by_alias=True))
    assert isinstance(rebuilt.audio_tracks[0].segments[0].media, SeparatedStem)


def test_stem_resolve_module_importable() -> None:
    """The helper module must be importable without demucs installed."""
    from eks_harness.video.compile import stem_resolve

    assert callable(stem_resolve.resolve_stem_path)
    assert callable(stem_resolve.apply_denoise)


def test_resolve_stem_path_missing_source_raises(tmp_path: Path) -> None:
    """A missing source file is surfaced before demucs is even invoked."""
    from eks_harness.video.compile.stem_resolve import resolve_stem_path

    stem = SeparatedStem(
        source=AudioFile(path=str(tmp_path / "does-not-exist.mp3")),
        stems=["vocals"],
    )
    with pytest.raises(FileNotFoundError):
        resolve_stem_path(stem, cache_dir=tmp_path)


def test_resolve_stem_path_runs_demucs(tmp_path: Path) -> None:
    """End-to-end heavy test, skipped if demucs is not installed."""
    pytest.importorskip("demucs")

    # Generate a 1-second silent stereo WAV as the input fixture.
    import wave

    src = tmp_path / "fixture.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 2)

    from eks_harness.video.compile.stem_resolve import resolve_stem_path

    stem = SeparatedStem(source=AudioFile(path=str(src)), stems=["vocals"])
    result = resolve_stem_path(stem, cache_dir=tmp_path / "cache")
    assert result.exists()
    # A second call must hit the cache: the stem path is the same and
    # demucs is not invoked again (we can't introspect that here, but
    # the result file must still exist and match).
    result2 = resolve_stem_path(stem, cache_dir=tmp_path / "cache")
    assert result2 == result
