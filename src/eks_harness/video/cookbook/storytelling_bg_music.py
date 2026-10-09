"""Voiceover storytelling clip with sidechain-ducked bg music and b-roll cuts.

Two audio tracks: voiceover and music. The music track sidechains off the
voiceover so it ducks automatically. Three b-roll segments cut on
sentence boundaries from the STT marker source.
"""

from eks_harness.video import (
    AudioFile,
    AudioSegment,
    AudioTrack,
    MarkerRef,
    Project,
    Seconds,
    Segment,
    SidechainConfig,
    STTMarkers,
    Track,
    VideoFile,
)

project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=24.0,
    markers=[
        STTMarkers(name="vo_sentences", source="audio_tracks[0]", backend="faster-whisper"),
    ],
    tracks=[
        Track(
            name="broll",
            segments=[
                Segment(
                    id="broll_0",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/broll_city.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=8.0),
                ),
                Segment(
                    id="broll_1",
                    start=MarkerRef(name="vo_sentences", index=1),
                    media=VideoFile(path="assets/broll_subway.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=8.0),
                ),
                Segment(
                    id="broll_2",
                    start=MarkerRef(name="vo_sentences", index=2),
                    media=VideoFile(path="assets/broll_skyline.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=8.0),
                ),
            ],
        ),
    ],
    audio_tracks=[
        AudioTrack(
            name="vo",
            segments=[
                AudioSegment(
                    id="voiceover",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/vo.wav"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=24.0),
                ),
            ],
        ),
        AudioTrack(
            name="music",
            sidechain=SidechainConfig(source="vo", threshold_db=-22.0, ratio=6.0),
            segments=[
                AudioSegment(
                    id="bed",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/bg_music.mp3"),
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
