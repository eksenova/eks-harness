"""Talking-head meme clip with TikTok-style captions and emphasis flashes.

Single vertical clip + STT marker source driving `Captions`. `Flash` lands
on a specific emphasis word so the audio "punch" coincides with the visual.
"""

from eks_harness.video import (
    Animated,
    Project,
    Seconds,
    Segment,
    STTMarkers,
    Track,
    VideoFile,
    WordRef,
)
from eks_harness.video.ir.effects import Captions, Flash

project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=18.0,
    markers=[
        STTMarkers(name="speech", source="tracks[0].segments[0]", backend="faster-whisper"),
    ],
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="head",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/talking_head.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=18.0),
                    effects=[
                        Captions(
                            source="speech",
                            style="tiktok",
                            position="bottom",
                            max_words_per_card=3,
                            size=72,
                            stroke=(0, 0, 0, 255),
                            stroke_width=4,
                        ),
                        Flash(
                            at=WordRef(text="absolutely", source="speech"),
                            duration=Animated[float](root=0.08),
                            color=(255, 240, 200),
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
