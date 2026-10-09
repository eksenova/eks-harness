"""Audio render pipeline: verify each effect emits the expected ffmpeg filter.

These tests capture the ``-filter_complex`` argument passed to
:func:`eks_harness.video.render.subprocess_runner.run_ffmpeg` by monkeypatching it.
That keeps tests hermetic - no real ffmpeg invocation - while exercising
the full :func:`render_audio_stem` lowering path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import (
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    Project,
    Seconds,
    Track,
)
from eks_harness.video.ir.audio_effects import (
    AudioFade,
    Ducking,
    EQBand,
    EQEffect,
    LoudnessNormalize,
    Reverb,
)
from eks_harness.video.render import audio as audio_module


@pytest.fixture
def captured_ffmpeg_args(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    captured: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], **_: Any) -> None:
        captured.append(list(args))

    monkeypatch.setattr(audio_module, "run_ffmpeg", fake_run_ffmpeg)
    return captured


def _video_only_project() -> Project:
    return Project(
        fps=30,
        resolution=(64, 64),
        duration=2.0,
        tracks=[Track(name="main", segments=[])],
    )


def _filter_complex_of(args: list[str]) -> str:
    idx = args.index("-filter_complex")
    return args[idx + 1]


def _seg(seg_id: str, **kw: Any) -> AudioSegment:
    return AudioSegment(
        id=seg_id,
        start=kw.pop("start", Seconds(t=0.0)),
        media=kw.pop("media", AudioFile(path=f"{seg_id}.wav")),
        in_=kw.pop("in_", Seconds(t=0.0)),
        out=kw.pop("out", Seconds(t=2.0)),
        **kw,
    )


def test_audio_fade_emits_afade_in_and_out(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.append(
        AudioTrack(
            name="music",
            segments=[_seg("clip", fade=AudioFade(fade_in=0.5, fade_out=0.75))],
        )
    )

    audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")

    graph = _filter_complex_of(captured_ffmpeg_args[0])
    assert "afade=t=in:st=0:d=0.500000" in graph
    assert "afade=t=out:st=1.250000:d=0.750000" in graph


def test_audio_fade_in_only(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.append(
        AudioTrack(
            name="music",
            segments=[_seg("clip", fade=AudioFade(fade_in=0.25))],
        )
    )

    audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")

    graph = _filter_complex_of(captured_ffmpeg_args[0])
    assert "afade=t=in:st=0:d=0.250000" in graph
    assert "afade=t=out" not in graph


def test_reverb_emits_aecho_with_three_taps(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.append(
        AudioTrack(
            name="music",
            segments=[
                _seg(
                    "clip",
                    reverb=Reverb(
                        wet=Animated[float](root=0.4),
                        room_size=0.5,
                        damping=0.5,
                    ),
                )
            ],
        )
    )

    audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")

    graph = _filter_complex_of(captured_ffmpeg_args[0])
    assert "aecho=" in graph
    # Three delay taps separated by '|'
    aecho_clause = next(part for part in graph.split(",") if "aecho=" in part)
    body = aecho_clause.split("aecho=", 1)[1].rstrip("[]")
    # Trim trailing label markers if present
    body = body.split("[")[0]
    parts = body.split(":")
    assert len(parts) == 4, f"expected 4 colon-separated parts, got: {parts}"
    delays = parts[2].split("|")
    decays = parts[3].split("|")
    assert len(delays) == 3
    assert len(decays) == 3


def test_segment_ducking_emits_sidechaincompress_keyed_on_source(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.extend(
        [
            AudioTrack(name="vo", segments=[_seg("vo_clip")]),
            AudioTrack(
                name="music",
                segments=[_seg("music_clip", ducking=Ducking(source="vo", ratio=8.0))],
            ),
        ]
    )

    audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")

    graph = _filter_complex_of(captured_ffmpeg_args[0])
    assert "sidechaincompress=" in graph
    assert "ratio=8.0" in graph
    assert "aseg_music_0_duck" in graph


def test_segment_ducking_rejects_unknown_source(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.append(
        AudioTrack(
            name="music",
            segments=[_seg("clip", ducking=Ducking(source="does_not_exist"))],
        )
    )

    with pytest.raises(ValueError, match="unknown audio track"):
        audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")


def test_loudness_normalize_emits_loudnorm_on_track_bus(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.append(
        AudioTrack(
            name="music",
            loudness=LoudnessNormalize(target_lufs=-16.0, lra=11.0, true_peak_db=-1.5),
            segments=[_seg("clip")],
        )
    )

    audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")

    graph = _filter_complex_of(captured_ffmpeg_args[0])
    assert "loudnorm=" in graph
    assert "I=-16.0" in graph
    assert "LRA=11.0" in graph
    assert "TP=-1.5" in graph


def test_parametric_eq_emits_one_equalizer_per_band(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.append(
        AudioTrack(
            name="music",
            parametric_eq=EQEffect(
                bands=[
                    EQBand(
                        frequency_hz=120.0,
                        gain_db=Animated[float](root=-3.0),
                        q=0.7,
                    ),
                    EQBand(
                        frequency_hz=3500.0,
                        gain_db=Animated[float](root=2.0),
                        q=1.2,
                    ),
                    EQBand(
                        frequency_hz=10000.0,
                        gain_db=Animated[float](root=1.5),
                        q=0.9,
                    ),
                ]
            ),
            segments=[_seg("clip")],
        )
    )

    audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")

    graph = _filter_complex_of(captured_ffmpeg_args[0])
    assert graph.count("equalizer=") == 3
    assert "f=120.0" in graph
    assert "f=3500.0" in graph
    assert "f=10000.0" in graph
    assert "t=q" in graph
    assert "g=-3.0" in graph
    assert "g=2.0" in graph
    assert "g=1.5" in graph


def test_all_effects_compose_in_one_render(
    tmp_path: Path,
    captured_ffmpeg_args: list[list[str]],
) -> None:
    project = _video_only_project()
    project.audio_tracks.extend(
        [
            AudioTrack(name="vo", segments=[_seg("vo_clip")]),
            AudioTrack(
                name="music",
                loudness=LoudnessNormalize(),
                parametric_eq=EQEffect(
                    bands=[
                        EQBand(frequency_hz=200.0, gain_db=Animated[float](root=-2.0))
                    ]
                ),
                segments=[
                    _seg(
                        "music_clip",
                        ducking=Ducking(source="vo"),
                        fade=AudioFade(fade_in=0.3, fade_out=0.3),
                        reverb=Reverb(wet=Animated[float](root=0.25)),
                    )
                ],
            ),
        ]
    )

    audio_module.render_audio_stem(project, MarkerSet(), tmp_path / "out.aac")

    graph = _filter_complex_of(captured_ffmpeg_args[0])
    assert "afade=t=in" in graph
    assert "afade=t=out" in graph
    assert "aecho=" in graph
    assert "sidechaincompress=" in graph
    assert "loudnorm=" in graph
    assert "equalizer=" in graph
