"""Lyric video: beat-driven flashes and downbeat-anchored line overlays.

A single background clip carries the project. :class:`BeatTracker` drives
a :class:`Flash` on every kick. Four lyric lines appear as
:class:`TextOverlay` segments anchored to successive downbeats via
:class:`BeatRef`. A :class:`Noise` curve modulates :class:`Brightness`
to give the background a subtle, sample-and-hold flicker that reads as
"energy" without distracting from the lyrics.
"""

from eks_harness.video import (
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    BeatPulse,
    BeatRef,
    BeatTracker,
    Brightness,
    ExpDecayEnv,
    Flash,
    Noise,
    Project,
    Seconds,
    Segment,
    TextOverlay,
    Track,
    VideoFile,
)

_DURATION = 16.0

_LYRIC_LINES: list[str] = [
    "feel the rhythm",
    "lose the noise",
    "find your fire",
    "ride the wave",
]


def _lyric_overlay(idx: int, text: str) -> TextOverlay:
    return TextOverlay(
        text=text,
        size=96,
        x=120,
        y=1500,
        color=(255, 255, 255, 255),
        start=BeatRef(stream="downbeat", every=idx + 1),
        duration=2.0,
    )


project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=_DURATION,
    markers=[
        BeatTracker(
            name="track",
            source="audio_tracks[0]",
            streams=["kick", "downbeat"],
            bpm=120.0,
        ),
    ],
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="bg",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/lyric_bg.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=_DURATION),
                    effects=[
                        Brightness(
                            amount=Animated[float](root=Noise(
                                seed=42,
                                min_value=-0.08,
                                max_value=0.08,
                                hold=0.25,
                            )),
                        ),
                        Flash(
                            at=BeatRef(
                                stream="kick",
                                range=(Seconds(t=0.0), Seconds(t=_DURATION)),
                            ),
                            duration=Animated[float](root=0.1),
                            color=(255, 255, 255),
                            curve=BeatPulse(
                                trigger=BeatRef(stream="kick"),
                                envelope=ExpDecayEnv(tau=0.09),
                                intensity_ramp=0.6,
                            ),
                        ),
                        *[
                            _lyric_overlay(i, line)
                            for i, line in enumerate(_LYRIC_LINES)
                        ],
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
                    id="song",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/lyric_song.mp3"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=_DURATION),
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
