"""Audio-first podcast clip with a generated waveform card and caption overlay.

Backs the visual with a generated text card (so no source video is needed),
applies an `RGBSplit` that pulses on every kick for visual interest, and
runs `Captions` from the STT marker source.
"""

from eks_harness.video import (
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    BeatPulse,
    BeatRef,
    BeatTracker,
    ExpDecayEnv,
    GeneratedCard,
    Project,
    Seconds,
    Segment,
    STTMarkers,
    Track,
)
from eks_harness.video.ir.effects import Captions, RGBSplit

project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=30.0,
    markers=[
        BeatTracker(name="podcast_beat", source="audio_tracks[0]", streams=["kick"]),
        STTMarkers(name="speech", source="audio_tracks[0]", backend="faster-whisper"),
    ],
    tracks=[
        Track(
            name="card",
            segments=[
                Segment(
                    id="cover",
                    start=Seconds(t=0.0),
                    media=GeneratedCard(
                        text="EP 042 - The Long Tail",
                        size=84,
                        fg=(240, 240, 240, 255),
                        bg=(20, 20, 32, 255),
                    ),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=30.0),
                    effects=[
                        RGBSplit(
                            offset_x=Animated[int](root=BeatPulse(
                                trigger=BeatRef(stream="kick"),
                                envelope=ExpDecayEnv(tau=0.12),
                                intensity_ramp=10.0,
                            )),
                        ),
                        Captions(
                            source="speech",
                            style="kinetic",
                            position="center",
                            size=64,
                            max_words_per_card=4,
                        ),
                    ],
                ),
            ],
        ),
    ],
    audio_tracks=[
        AudioTrack(
            name="podcast",
            segments=[
                AudioSegment(
                    id="ep",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/podcast_42.wav"),
                    in_=Seconds(t=120.0),
                    out=Seconds(t=150.0),
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
