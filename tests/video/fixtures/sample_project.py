"""Canary sample project: brightness pulses on every kick."""

from eks_harness.video import (
    Animated,
    BeatPulse,
    BeatRef,
    BeatTracker,
    Brightness,
    EaseInOut,
    ExpDecayEnv,
    ImageFile,
    Keyframe,
    Project,
    Seconds,
    Segment,
    Track,
)


project = Project(
    fps=60,
    resolution=(1080, 1920),
    duration=16.0,
    markers=[
        BeatTracker(
            name="kicks",
            source="audio_tracks[0]",
            streams=["kick"],
            bpm=120.0,
        ),
    ],
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="hero",
                    start=Seconds(t=0.0),
                    media=ImageFile(path="poster.png"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=16.0),
                    effects=[
                        Brightness(
                            amount=Animated[float](
                                root=BeatPulse(
                                    trigger=BeatRef(
                                        stream="kick",
                                        range=(Seconds(t=0.0), Seconds(t=16.0)),
                                    ),
                                    envelope=ExpDecayEnv(tau=0.08),
                                    intensity_ramp=[
                                        Keyframe[float](t=Seconds(t=0.0), v=0.1, easing=EaseInOut()),
                                        Keyframe[float](t=Seconds(t=16.0), v=0.9),
                                    ],
                                )
                            )
                        ),
                    ],
                ),
            ],
        ),
    ],
)
