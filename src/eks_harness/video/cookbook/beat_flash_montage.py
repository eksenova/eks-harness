"""Beat-synced flash montage for vertical short-form delivery.

Four video clips back-to-back over a music bed; a kick-detector marker
source drives a `Flash` effect on every kick using `BeatPulse` to ramp the
flash intensity over the 16-second window.
"""

from eks_harness.video import (
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    BeatPulse,
    BeatRef,
    BeatTracker,
    EaseInOut,
    ExpDecayEnv,
    Flash,
    Keyframe,
    Project,
    Seconds,
    Segment,
    Track,
    VideoFile,
)


def _clip(idx: int, start: float) -> Segment:
    return Segment(
        id=f"clip_{idx}",
        start=Seconds(t=start),
        media=VideoFile(path=f"assets/clip_{idx}.mp4"),
        in_=Seconds(t=0.0),
        out=Seconds(t=4.0),
    )


def _flash_track() -> Track:
    return Track(
        name="flash",
        z=10,
        segments=[
            Segment(
                id="flash_overlay",
                start=Seconds(t=0.0),
                media=VideoFile(path="assets/clip_0.mp4"),
                in_=Seconds(t=0.0),
                out=Seconds(t=16.0),
                effects=[
                    Flash(
                        at=BeatRef(stream="kick", range=(Seconds(t=0.0), Seconds(t=16.0))),
                        duration=Animated[float](root=0.12),
                        color=(255, 255, 255),
                        curve=BeatPulse(
                            trigger=BeatRef(stream="kick"),
                            envelope=ExpDecayEnv(tau=0.08),
                            intensity_ramp=[
                                Keyframe[float](t=Seconds(t=0.0), v=0.2, easing=EaseInOut()),
                                Keyframe[float](t=Seconds(t=16.0), v=0.9),
                            ],
                        ),
                    ),
                ],
            ),
        ],
    )


project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=16.0,
    markers=[
        BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"], bpm=128.0),
    ],
    tracks=[
        Track(
            name="main",
            segments=[_clip(i, i * 4.0) for i in range(4)],
        ),
        _flash_track(),
    ],
    audio_tracks=[
        AudioTrack(
            name="music",
            segments=[
                AudioSegment(
                    id="bed",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/music.mp3"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=16.0),
                ),
            ],
        ),
    ],
)


if __name__ == "__main__":
    from pathlib import Path

    Path("./out").mkdir(exist_ok=True)
    Path("./out/project.json").write_text(
        project.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
