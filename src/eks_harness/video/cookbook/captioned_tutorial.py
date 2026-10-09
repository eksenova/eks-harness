"""Screen-recording tutorial with subtitle-style captions and per-sentence punch-ins.

`STTMarkers` produces a `sentence` stream the renderer can resolve as
`MarkerRef`. `PunchIn` zooms toward the speaker on each sentence boundary;
the captioner runs in subtitle (non-kinetic) mode for readability.
"""

from eks_harness.video import (
    Animated,
    Project,
    Seconds,
    Segment,
    STTMarkers,
    Track,
    VideoFile,
)
from eks_harness.video.ir.effects import Captions, PunchIn

project = Project(
    fps=30,
    resolution=(1920, 1080),
    duration=45.0,
    markers=[
        STTMarkers(name="speech", source="tracks[0].segments[0]", backend="faster-whisper"),
    ],
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="screencast",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/screen_recording.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=45.0),
                    effects=[
                        PunchIn(
                            track_speaker=False,
                            zoom=Animated[float](root=1.15),
                        ),
                        Captions(
                            source="speech",
                            style="subtitle",
                            position="bottom",
                            margin=80,
                            size=44,
                            color=(255, 255, 255, 255),
                            stroke=(0, 0, 0, 200),
                            stroke_width=3,
                            max_words_per_card=8,
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
