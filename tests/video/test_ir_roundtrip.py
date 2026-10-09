from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from eks_harness.video import (
    EQ,
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    BackOut,
    BeatRef,
    BeatTracker,
    BounceOut,
    BoxEnv,
    Brightness,
    Crop,
    Crossfade,
    CubicBezier,
    Cut,
    EaseIn,
    EaseInCubic,
    EaseInOut,
    EaseInOutCubic,
    EaseOut,
    EaseOutCubic,
    ElasticOut,
    ExpDecay,
    ExpDecayEnv,
    Fade,
    Flash,
    Frames,
    GeneratedCard,
    ImageFile,
    Keyframe,
    Lambda,
    LinearEasing,
    LinearEnv,
    MarkerRef,
    PluginEasing,
    PluginRequirement,
    PluginTransition,
    Project,
    RenderSettings,
    SceneMarkers,
    Seconds,
    Segment,
    SidechainConfig,
    Sine,
    Solid,
    STTMarkers,
    Track,
    TTSGenerated,
    VideoFile,
    WordRef,
)
from eks_harness.video.ir.curves import LFO


def _round_trip(value: BaseModel) -> None:
    payload = value.model_dump(by_alias=True, mode="json")
    rebuilt = type(value).model_validate(payload)
    assert rebuilt == value
    text = json.dumps(payload)
    rebuilt2 = type(value).model_validate(json.loads(text))
    assert rebuilt2 == value


@pytest.mark.parametrize(
    "value",
    [
        Seconds(t=1.5),
        Frames(n=120),
        BeatRef(stream="kick", every=2),
        WordRef(text="hello"),
        MarkerRef(name="intro"),
        LinearEasing(),
        EaseIn(),
        EaseOut(),
        EaseInOut(),
        EaseInCubic(),
        EaseOutCubic(),
        EaseInOutCubic(),
        BounceOut(),
        ElasticOut(),
        BackOut(),
        CubicBezier(p1x=0.42, p1y=0.0, p2x=0.58, p2y=1.0),
        PluginEasing(name="custom", params={"alpha": 0.5}),
        ExpDecayEnv(tau=0.08),
        LinearEnv(rise=0.05, fall=0.2),
        BoxEnv(width=0.1),
        ExpDecay(tau=0.5),
        Sine(freq_hz=2.0),
        LFO(shape="tri", freq_hz=1.0),
        Lambda(expr="t*2"),
        VideoFile(path="a.mp4"),
        ImageFile(path="a.png"),
        Solid(color=(0, 0, 0, 255)),
        GeneratedCard(text="hi"),
        AudioFile(path="b.wav"),
        TTSGenerated(text="hello world"),
        Cut(),
        Crossfade(duration=0.5),
        PluginTransition(name="warp", params={"k": 1}),
        EQ(low_db=2.0),
        SidechainConfig(source="vo"),
        Brightness(amount=Animated[float](root=0.4)),
        Flash(at=Seconds(t=1.0), duration=Animated[float](root=0.1)),
        Crop(
            x=Animated[int](root=0),
            y=Animated[int](root=0),
            w=Animated[int](root=512),
            h=Animated[int](root=512),
        ),
        Fade(direction="in", duration=0.5),
        BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"], bpm=120.0),
        STTMarkers(name="vo", source="audio_tracks[0]"),
        SceneMarkers(name="cuts", source="tracks[0]"),
        Keyframe[float](t=Seconds(t=0.0), v=0.1, easing=EaseInOut()),
        RenderSettings(),
        PluginRequirement(name="cool", version_spec=">=1.0"),
    ],
)
def test_round_trip(value: BaseModel) -> None:
    _round_trip(value)


def test_animated_constant_round_trip() -> None:
    a = Animated[float](root=1.5)
    payload = a.model_dump(mode="json")
    rebuilt = Animated[float].model_validate(payload)
    assert rebuilt.root == 1.5


def test_animated_keyframes_round_trip() -> None:
    a = Animated[float](
        root=[
            Keyframe[float](t=Seconds(t=0.0), v=0.0),
            Keyframe[float](t=Seconds(t=1.0), v=1.0),
        ]
    )
    payload = a.model_dump(mode="json")
    rebuilt = Animated[float].model_validate(payload)
    assert isinstance(rebuilt.root, list)
    assert len(rebuilt.root) == 2


def test_full_project_round_trip() -> None:
    project = Project(
        fps=60,
        resolution=(1080, 1920),
        duration=4.0,
        markers=[BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"], bpm=120.0)],
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="poster.png"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=4.0),
                        effects=[
                            Brightness(amount=Animated[float](root=0.4)),
                        ],
                    ),
                ],
            ),
        ],
        audio_tracks=[
            AudioTrack(
                name="music",
                segments=[
                    AudioSegment(
                        id="m1",
                        start=Seconds(t=0.0),
                        media=AudioFile(path="music.wav"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=4.0),
                    ),
                ],
            ),
        ],
    )
    payload = project.model_dump_json(by_alias=True)
    rebuilt = Project.model_validate_json(payload)
    assert rebuilt.duration == project.duration
    assert rebuilt.tracks[0].segments[0].id == "hero"


def test_segment_string_time_coercion() -> None:
    project = Project(
        fps=60,
        resolution=(1080, 1920),
        duration=4.0,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start="0:00",  # type: ignore[arg-type]
                        media=ImageFile(path="poster.png"),
                        in_="0:00",  # type: ignore[arg-type]
                        out="0:04",  # type: ignore[arg-type]
                    )
                ],
            )
        ],
    )
    seg = project.tracks[0].segments[0]
    assert isinstance(seg.start, Seconds)
    assert seg.out == Seconds(t=4.0)


def test_marker_reference_validation_rejects_unknown_stream() -> None:
    with pytest.raises(ValueError):
        Project(
            fps=60,
            resolution=(1080, 1920),
            duration=4.0,
            tracks=[
                Track(
                    name="main",
                    segments=[
                        Segment(
                            id="hero",
                            start=Seconds(t=0.0),
                            media=ImageFile(path="poster.png"),
                            in_=Seconds(t=0.0),
                            out=Seconds(t=4.0),
                            effects=[
                                Flash(
                                    at=BeatRef(stream="kick"),
                                    duration=Animated[float](root=0.1),
                                ),
                            ],
                        )
                    ],
                )
            ],
        )
