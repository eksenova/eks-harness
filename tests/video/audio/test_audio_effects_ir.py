"""Round-trip tests for audio effect IR models.

Verifies every audio effect model preserves its data through
``model_validate(model.model_dump())`` and that the typed fields on
:class:`AudioSegment` / :class:`AudioTrack` accept and reject as expected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from eks_harness.video.ir import (
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    Seconds,
)
from eks_harness.video.ir.audio_effects import (
    AudioFade,
    Ducking,
    EQBand,
    EQEffect,
    LoudnessNormalize,
    Reverb,
)


def test_ducking_round_trip() -> None:
    effect = Ducking(
        source="vo",
        threshold_db=-18.0,
        ratio=6.0,
        attack_ms=5.0,
        release_ms=150.0,
    )
    rebuilt = Ducking.model_validate(effect.model_dump())
    assert rebuilt == effect


def test_ducking_defaults() -> None:
    effect = Ducking(source="vo")
    assert effect.threshold_db == -20.0
    assert effect.ratio == 4.0
    assert effect.attack_ms == 10.0
    assert effect.release_ms == 200.0


def test_loudness_normalize_round_trip() -> None:
    effect = LoudnessNormalize(target_lufs=-14.0, true_peak_db=-1.0, lra=9.0)
    rebuilt = LoudnessNormalize.model_validate(effect.model_dump())
    assert rebuilt == effect


def test_loudness_normalize_defaults_target_short_form() -> None:
    effect = LoudnessNormalize()
    assert effect.target_lufs == -16.0
    assert effect.true_peak_db == -1.5
    assert effect.lra == 11.0


def test_audio_fade_round_trip() -> None:
    effect = AudioFade(fade_in=0.5, fade_out=1.25)
    rebuilt = AudioFade.model_validate(effect.model_dump())
    assert rebuilt == effect


def test_eq_band_round_trip_with_animated_gain() -> None:
    band = EQBand(
        frequency_hz=2500.0,
        gain_db=Animated[float](root=3.0),
        q=1.4,
        type="peak",
    )
    rebuilt = EQBand.model_validate(band.model_dump())
    assert rebuilt.frequency_hz == 2500.0
    assert rebuilt.gain_db.root == 3.0
    assert rebuilt.q == 1.4
    assert rebuilt.type == "peak"


def test_eq_effect_round_trip() -> None:
    effect = EQEffect(
        bands=[
            EQBand(frequency_hz=80.0, gain_db=Animated[float](root=-2.0), type="low_shelf"),
            EQBand(frequency_hz=3500.0, gain_db=Animated[float](root=2.5), q=0.8),
            EQBand(frequency_hz=10000.0, gain_db=Animated[float](root=1.5), type="high_shelf"),
        ]
    )
    rebuilt = EQEffect.model_validate(effect.model_dump())
    assert len(rebuilt.bands) == 3
    assert rebuilt.bands[0].type == "low_shelf"
    assert rebuilt.bands[2].type == "high_shelf"


def test_reverb_round_trip() -> None:
    effect = Reverb(
        wet=Animated[float](root=0.45),
        room_size=0.7,
        damping=0.3,
    )
    rebuilt = Reverb.model_validate(effect.model_dump())
    assert rebuilt.wet.root == 0.45
    assert rebuilt.room_size == 0.7
    assert rebuilt.damping == 0.3


def test_audio_segment_carries_effects_round_trip() -> None:
    segment = AudioSegment(
        id="clip",
        start=Seconds(t=0.0),
        media=AudioFile(path="x.wav"),
        in_=Seconds(t=0.0),
        out=Seconds(t=3.0),
        ducking=Ducking(source="vo"),
        fade=AudioFade(fade_in=0.25, fade_out=0.5),
        reverb=Reverb(wet=Animated[float](root=0.4)),
    )
    rebuilt = AudioSegment.model_validate(segment.model_dump(by_alias=True))
    assert rebuilt.ducking is not None and rebuilt.ducking.source == "vo"
    assert rebuilt.fade is not None and rebuilt.fade.fade_in == 0.25
    assert rebuilt.reverb is not None and rebuilt.reverb.wet.root == 0.4


def test_audio_track_carries_effects_round_trip() -> None:
    track = AudioTrack(
        name="music",
        loudness=LoudnessNormalize(target_lufs=-14.0),
        parametric_eq=EQEffect(
            bands=[
                EQBand(frequency_hz=120.0, gain_db=Animated[float](root=-3.0)),
            ]
        ),
    )
    rebuilt = AudioTrack.model_validate(track.model_dump())
    assert rebuilt.loudness is not None and rebuilt.loudness.target_lufs == -14.0
    assert rebuilt.parametric_eq is not None
    assert rebuilt.parametric_eq.bands[0].frequency_hz == 120.0


def test_audio_effects_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Ducking.model_validate({"source": "vo", "rogue": 1})
    with pytest.raises(ValidationError):
        LoudnessNormalize.model_validate({"target_lufs": -16.0, "rogue": 1})
    with pytest.raises(ValidationError):
        AudioFade.model_validate({"fade_in": 0.1, "rogue": 1})
    with pytest.raises(ValidationError):
        EQBand.model_validate({"frequency_hz": 100.0, "rogue": 1})
    with pytest.raises(ValidationError):
        EQEffect.model_validate({"bands": [], "rogue": 1})
    with pytest.raises(ValidationError):
        Reverb.model_validate({"room_size": 0.5, "rogue": 1})
