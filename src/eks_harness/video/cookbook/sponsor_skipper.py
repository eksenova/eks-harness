"""Hand-authored sponsor skip using silence-anchored cut points.

A single long-form source is reassembled into two adjacent segments
that skip a known sponsor break. The cut points (in this recipe, 62.0s
and 105.0s of the source) come from a human listening pass - typically
landing on silent regions either side of the read so the join is
inaudible.

:class:`SilenceMarkers` is declared so a future automated sponsor
detector can consume it: silent-region times are exactly the
candidate cut boundaries. Automatic sponsor detection (e.g. matching
silence patterns against SponsorBlock-style segments) is a TODO; this
recipe shows the *manual* cut shape that automation would emit.

The two adjacent segments use the default :class:`Cut` transitions -
back-to-back hard cuts that, when picked on silent boundaries, are
imperceptible. Audio is split the same way so video and audio stay
in lockstep.
"""

from eks_harness.video import (
    AudioFile,
    AudioSegment,
    AudioTrack,
    Cut,
    Project,
    Seconds,
    Segment,
    SilenceMarkers,
    Track,
    VideoFile,
)

_SOURCE = "assets/longform.mp4"
_SOURCE_AUDIO = "assets/longform.wav"

_PRE_IN = 0.0
_PRE_OUT = 62.0

_POST_IN = 105.0
_POST_OUT = 240.0

_PRE_DURATION = _PRE_OUT - _PRE_IN
_POST_DURATION = _POST_OUT - _POST_IN
_TOTAL = _PRE_DURATION + _POST_DURATION


project = Project(
    fps=30,
    resolution=(1920, 1080),
    duration=_TOTAL,
    markers=[
        SilenceMarkers(
            name="silences",
            source="audio_tracks[0]",
            threshold_db=-40.0,
            min_duration=0.4,
        ),
    ],
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="pre_sponsor",
                    start=Seconds(t=0.0),
                    media=VideoFile(path=_SOURCE),
                    in_=Seconds(t=_PRE_IN),
                    out=Seconds(t=_PRE_OUT),
                    transition_out=Cut(),
                ),
                Segment(
                    id="post_sponsor",
                    start=Seconds(t=_PRE_DURATION),
                    media=VideoFile(path=_SOURCE),
                    in_=Seconds(t=_POST_IN),
                    out=Seconds(t=_POST_OUT),
                    transition_in=Cut(),
                ),
            ],
        ),
    ],
    audio_tracks=[
        AudioTrack(
            name="vo",
            segments=[
                AudioSegment(
                    id="pre_sponsor_audio",
                    start=Seconds(t=0.0),
                    media=AudioFile(path=_SOURCE_AUDIO),
                    in_=Seconds(t=_PRE_IN),
                    out=Seconds(t=_PRE_OUT),
                ),
                AudioSegment(
                    id="post_sponsor_audio",
                    start=Seconds(t=_PRE_DURATION),
                    media=AudioFile(path=_SOURCE_AUDIO),
                    in_=Seconds(t=_POST_IN),
                    out=Seconds(t=_POST_OUT),
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
