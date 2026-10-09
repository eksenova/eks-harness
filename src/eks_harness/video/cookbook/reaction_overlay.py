"""Reaction-video PIP composition with two overlay strategies.

The main vertical clip carries full-frame action. A reactor is introduced
via a :class:`LowerThird` lower-third graphic, then their face is shown
two ways:

1. A static thumbnail composited via :class:`Watermark` (which accepts a
   :class:`pathlib.Path` to any image). Cheap and renders inside the
   ffmpeg filter graph.
2. The "proper" video picture-in-picture: a second :class:`Track` with
   ``z=10`` holding the live reaction clip, scaled and positioned via
   :class:`Zoom` + :class:`Pan` so it sits in the upper-right corner.

The track-based version is the right approach when the reactor's
expression matters frame-to-frame; the watermark is fine when a static
brand thumbnail suffices.
"""

from eks_harness.video import (
    Animated,
    LowerThird,
    Pan,
    Project,
    Seconds,
    Segment,
    Track,
    VideoFile,
    Watermark,
    Zoom,
)

_DURATION = 20.0
_PIP_SCALE = 0.28
_PIP_X = 640
_PIP_Y = -700


project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=_DURATION,
    tracks=[
        Track(
            name="main",
            z=0,
            segments=[
                Segment(
                    id="primary",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/main_clip.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=_DURATION),
                    effects=[
                        Watermark(
                            path="assets/reaction_thumb.png",
                            x=Animated[int](root=720),
                            y=Animated[int](root=120),
                            scale=Animated[float](root=0.6),
                            opacity=Animated[float](root=0.95),
                        ),
                        LowerThird(
                            title="@reactor_handle",
                            subtitle="watching live",
                            position="left",
                            margin=140,
                        ),
                    ],
                ),
            ],
        ),
        Track(
            name="reaction_pip",
            z=10,
            segments=[
                Segment(
                    id="reaction",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/reaction_clip.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=_DURATION),
                    effects=[
                        Zoom(scale=Animated[float](root=_PIP_SCALE)),
                        Pan(
                            dx=Animated[int](root=_PIP_X),
                            dy=Animated[int](root=_PIP_Y),
                        ),
                    ],
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
