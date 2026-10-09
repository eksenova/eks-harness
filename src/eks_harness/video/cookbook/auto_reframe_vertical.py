"""Convert a landscape source to a vertical 9:16 deliverable via face tracking.

`AutoReframe` produces a per-frame crop window from a face/salience tracker
and the orchestrator stitches the frame-pipeline output back into a
`tiktok-1080-h264` encode.
"""

from eks_harness.video import (
    Project,
    RenderSettings,
    Seconds,
    Segment,
    Track,
    VideoFile,
)
from eks_harness.video.ir.effects import AutoReframe

project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=22.0,
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="landscape",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/interview_1080p.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=22.0),
                    effects=[
                        AutoReframe(target_aspect=(9, 16), tracker="face", smoothing=0.9),
                    ],
                ),
            ],
        ),
    ],
    render_settings=RenderSettings(preset="tiktok-1080-h264", quality_profile="final"),
)


if __name__ == "__main__":
    from pathlib import Path

    Path("./out").mkdir(exist_ok=True)
    Path("./out/project.json").write_text(
        project.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
