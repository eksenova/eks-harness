"""Side-by-side before/after comparison framed by diagonal wipe transitions.

A short full-frame "before" segment wipes to a split-screen comparison
where two tracks each render half the frame via :class:`Crop`, each
labelled with a :class:`TextOverlay`. A second wipe reveals the final
full-frame "after". Vertical 1080x1920 layout for short-form delivery.
"""

from eks_harness.video import (
    Crop,
    Project,
    Seconds,
    Segment,
    TextOverlay,
    Track,
    VideoFile,
    Wipe,
)

_HALF_WIDTH = 540
_HEIGHT = 1920


def _split_half(side: str, source: str) -> Segment:
    is_left = side == "left"
    return Segment(
        id=f"split_{side}",
        start=Seconds(t=3.0),
        media=VideoFile(path=source),
        in_=Seconds(t=0.0),
        out=Seconds(t=7.0),
        effects=[
            Crop(
                x=0 if is_left else _HALF_WIDTH,
                y=0,
                w=_HALF_WIDTH,
                h=_HEIGHT,
            ),
            TextOverlay(
                text="BEFORE" if is_left else "AFTER",
                size=84,
                x=120 if is_left else _HALF_WIDTH + 120,
                y=160,
                color=(255, 255, 255, 255),
            ),
        ],
    )


project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=12.0,
    tracks=[
        Track(
            name="intro",
            z=0,
            segments=[
                Segment(
                    id="before_full",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/before.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=3.0),
                    transition_out=Wipe(duration=0.4, direction="right"),
                ),
                Segment(
                    id="after_full",
                    start=Seconds(t=10.0),
                    media=VideoFile(path="assets/after.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=2.0),
                    transition_in=Wipe(duration=0.4, direction="left"),
                ),
            ],
        ),
        Track(
            name="split_left",
            z=1,
            segments=[_split_half("left", "assets/before.mp4")],
        ),
        Track(
            name="split_right",
            z=1,
            segments=[_split_half("right", "assets/after.mp4")],
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
