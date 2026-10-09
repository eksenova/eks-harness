"""Ken Burns slideshow: six stills with slow zoom + pan, crossfaded over music.

Each image holds for five seconds with :class:`Zoom` keyframed from 1.0
to 1.15 and a gentle :class:`Pan` drift, alternating horizontal direction
for visual variety. :class:`Crossfade` ties every adjacent pair so the
sequence reads as one continuous montage. Backed by a single music bed
on an :class:`AudioTrack`.
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
    Pan,
    Project,
    Seconds,
    Segment,
    Track,
    Zoom,
)

_SLIDE_COUNT = 6
_SLIDE_DURATION = 5.0
_TOTAL = _SLIDE_COUNT * _SLIDE_DURATION
_CROSSFADE = 0.6


def _slide(idx: int) -> Segment:
    start = idx * _SLIDE_DURATION
    pan_direction = 1 if idx % 2 == 0 else -1
    return Segment(
        id=f"slide_{idx}",
        start=Seconds(t=start),
        media=ImageFile(path=f"assets/photo_{idx}.jpg"),
        in_=Seconds(t=0.0),
        out=Seconds(t=_SLIDE_DURATION),
        effects=[
            Zoom(
                scale=Animated[float](root=[
                    Keyframe[float](t=Seconds(t=0.0), v=1.0, easing=EaseInOutCubic()),
                    Keyframe[float](t=Seconds(t=_SLIDE_DURATION), v=1.15),
                ]),
            ),
            Pan(
                dx=Animated[int](root=[
                    Keyframe[int](t=Seconds(t=0.0), v=0, easing=EaseInOutCubic()),
                    Keyframe[int](t=Seconds(t=_SLIDE_DURATION), v=60 * pan_direction),
                ]),
                dy=Animated[int](root=[
                    Keyframe[int](t=Seconds(t=0.0), v=0, easing=EaseInOutCubic()),
                    Keyframe[int](t=Seconds(t=_SLIDE_DURATION), v=20),
                ]),
            ),
        ],
        transition_in=Crossfade(duration=_CROSSFADE) if idx > 0 else Cut(),
        transition_out=(
            Crossfade(duration=_CROSSFADE) if idx < _SLIDE_COUNT - 1 else Cut()
        ),
    )


project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=_TOTAL,
    tracks=[
        Track(
            name="main",
            segments=[_slide(i) for i in range(_SLIDE_COUNT)],
        ),
    ],
    audio_tracks=[
        AudioTrack(
            name="music",
            segments=[
                AudioSegment(
                    id="bed",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/slideshow_bed.mp3"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=_TOTAL),
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
