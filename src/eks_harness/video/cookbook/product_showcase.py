"""Product showcase: still images zoomed and crossfaded over a music bed.

Three product photos play for 4s each with a `Zoom` keyframed from 1.0 to
1.15, mirrored horizontally for visual variety, color graded with a LUT.
Crossfade transitions tie the segments together.
"""

from eks_harness.video import (
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    Crossfade,
    Cut,
    EaseInOutCubic,
    ImageFile,
    Keyframe,
    Project,
    Seconds,
    Segment,
    Track,
)
from eks_harness.video.ir.effects import ColorGrade, Mirror, Zoom


def _showcase_segment(idx: int, start: float, mirrored: bool) -> Segment:
    effects = [
        Zoom(
            scale=Animated[float](root=[
                Keyframe[float](t=Seconds(t=0.0), v=1.0, easing=EaseInOutCubic()),
                Keyframe[float](t=Seconds(t=4.0), v=1.15),
            ]),
        ),
        ColorGrade(
            lut_path="assets/luts/warm_filmic.cube",
            exposure=0.1,
            temperature=0.05,
            tint=0.0,
        ),
    ]
    if mirrored:
        effects.append(Mirror(axis="horizontal"))
    return Segment(
        id=f"product_{idx}",
        start=Seconds(t=start),
        media=ImageFile(path=f"assets/product_{idx}.jpg"),
        in_=Seconds(t=0.0),
        out=Seconds(t=4.0),
        effects=effects,
        transition_in=Crossfade(duration=0.4) if idx > 0 else Cut(),
        transition_out=Crossfade(duration=0.4) if idx < 2 else Cut(),
    )


project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=12.0,
    tracks=[
        Track(
            name="main",
            segments=[
                _showcase_segment(0, 0.0, mirrored=False),
                _showcase_segment(1, 4.0, mirrored=True),
                _showcase_segment(2, 8.0, mirrored=False),
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
                    media=AudioFile(path="assets/uplifting_loop.mp3"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=12.0),
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
