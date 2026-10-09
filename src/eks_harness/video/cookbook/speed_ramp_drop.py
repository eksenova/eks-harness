"""Slow-mo speed ramp synced to a music drop.

The clip plays at 1.0x until the drop (anchored to the kick on beat 16),
ramps to 0.3x with `ease_out_cubic`, then accelerates back to 1.0x for the
back half. The `BeatTracker` provides the drop reference.
"""

from eks_harness.video import (
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    BeatTracker,
    EaseInOutCubic,
    EaseOutCubic,
    Keyframe,
    Project,
    Seconds,
    Segment,
    Track,
    VideoFile,
)
from eks_harness.video.ir.effects import SpeedRamp

project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=24.0,
    markers=[
        BeatTracker(name="track", source="audio_tracks[0]", streams=["kick", "downbeat"]),
    ],
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="action",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/parkour.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=24.0),
                    effects=[
                        SpeedRamp(
                            factor=Animated[float](root=[
                                Keyframe[float](t=Seconds(t=0.0), v=1.0),
                                Keyframe[float](t=Seconds(t=11.5), v=1.0, easing=EaseOutCubic()),
                                Keyframe[float](t=Seconds(t=12.0), v=0.3),
                                Keyframe[float](t=Seconds(t=13.5), v=0.3, easing=EaseInOutCubic()),
                                Keyframe[float](t=Seconds(t=15.0), v=1.0),
                            ]),
                        ),
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
                    id="bed",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/drop.mp3"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=24.0),
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
