"""Round-trip tests for the DeepFilterDenoise audio effect.

The heavy inference is skipped unless ``deepfilternet`` is installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eks_harness.video import (
    AudioFile,
    AudioSegment,
    AudioTrack,
    DeepFilterDenoise,
    Project,
    Seconds,
)


def test_deepfilter_denoise_defaults() -> None:
    effect = DeepFilterDenoise()
    assert effect.enabled is True
    assert effect.attenuation_limit_db == 60.0


def test_deepfilter_denoise_round_trip() -> None:
    effect = DeepFilterDenoise(enabled=True, attenuation_limit_db=30.0)
    rebuilt = DeepFilterDenoise.model_validate(effect.model_dump())
    assert rebuilt == effect


def test_deepfilter_denoise_negative_attenuation_rejected() -> None:
    with pytest.raises(ValidationError):
        DeepFilterDenoise(attenuation_limit_db=-5.0)


def test_deepfilter_denoise_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        DeepFilterDenoise.model_validate(
            {"enabled": True, "attenuation_limit_db": 40.0, "unknown": 1}
        )


def test_audio_segment_with_denoise_field() -> None:
    seg = AudioSegment(
        id="vo",
        start=Seconds(t=0),
        media=AudioFile(path="vo.wav"),
        in_=Seconds(t=0),
        out=Seconds(t=5),
        denoise=DeepFilterDenoise(enabled=True),
    )
    rebuilt = AudioSegment.model_validate(seg.model_dump(by_alias=True))
    assert rebuilt.denoise is not None
    assert rebuilt.denoise.enabled is True
    assert rebuilt.denoise.attenuation_limit_db == 60.0


def test_audio_segment_denoise_defaults_to_none() -> None:
    seg = AudioSegment(
        id="vo",
        start=Seconds(t=0),
        media=AudioFile(path="vo.wav"),
        in_=Seconds(t=0),
        out=Seconds(t=5),
    )
    assert seg.denoise is None


def test_project_with_denoised_segment_round_trip() -> None:
    project = Project(
        fps=30,
        resolution=(1080, 1920),
        duration=10.0,
        tracks=[],
        audio_tracks=[
            AudioTrack(
                name="vo",
                segments=[
                    AudioSegment(
                        id="line",
                        start=Seconds(t=0),
                        media=AudioFile(path="vo.wav"),
                        in_=Seconds(t=0),
                        out=Seconds(t=5),
                        denoise=DeepFilterDenoise(
                            enabled=True, attenuation_limit_db=45.0
                        ),
                    )
                ],
            )
        ],
    )
    rebuilt = Project.model_validate(project.model_dump(by_alias=True))
    seg = rebuilt.audio_tracks[0].segments[0]
    assert seg.denoise is not None
    assert seg.denoise.attenuation_limit_db == 45.0


def test_apply_denoise_missing_source_raises(tmp_path: Path) -> None:
    """A missing source surfaces before DeepFilterNet is invoked."""
    from eks_harness.video.compile.stem_resolve import apply_denoise

    missing = tmp_path / "missing.wav"
    with pytest.raises(FileNotFoundError):
        apply_denoise(
            missing, DeepFilterDenoise(enabled=True), cache_dir=tmp_path
        )


def test_apply_denoise_runs_inference(tmp_path: Path) -> None:
    """End-to-end heavy test, skipped if deepfilternet is not installed."""
    pytest.importorskip("df")

    import wave

    src = tmp_path / "fixture.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 48000)

    from eks_harness.video.compile.stem_resolve import apply_denoise

    out = apply_denoise(
        src, DeepFilterDenoise(enabled=True), cache_dir=tmp_path / "cache"
    )
    assert out.exists()
    out2 = apply_denoise(
        src, DeepFilterDenoise(enabled=True), cache_dir=tmp_path / "cache"
    )
    assert out2 == out
