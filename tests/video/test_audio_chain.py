"""AudioTrack -> ffmpeg filter chain tests."""

from __future__ import annotations

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import (
    EQ,
    Animated,
    AudioFile,
    AudioSegment,
    AudioTrack,
    Project,
    Seconds,
    SidechainConfig,
    Track,
)
from eks_harness.video.render.ffmpeg_builder import build_audio_track_chain


def _project() -> Project:
    return Project(
        fps=30,
        resolution=(64, 64),
        duration=2.0,
        tracks=[Track(name="main", segments=[])],
    )


def test_audio_chain_includes_eq_volume_and_sidechain() -> None:
    track = AudioTrack(
        name="music",
        gain_db=Animated[float](root=-3.0),
        eq=Animated[EQ](root=EQ(low_db=2.0, mid_db=-1.0, high_db=4.0)),
        sidechain=SidechainConfig(source="vo"),
        segments=[
            AudioSegment(
                id="a",
                start=Seconds(t=0.0),
                media=AudioFile(path="m.wav"),
                in_=Seconds(t=0.0),
                out=Seconds(t=2.0),
            )
        ],
    )
    chain = build_audio_track_chain(track, _project(), MarkerSet())
    serialized = chain.serialize()

    assert "equalizer=f=100.0:t=h:w=200.0:g=2.0" in serialized
    assert "equalizer=f=1000.0:t=h:w=400.0:g=-1.0" in serialized
    assert "equalizer=f=8000.0:t=h:w=2000.0:g=4.0" in serialized
    assert "volume=volume='pow(10,(-3.000000)/20)':eval=frame:precision=float" in serialized
    assert "sidechaincompress=" in serialized


def test_audio_chain_minimal_track_has_only_volume() -> None:
    track = AudioTrack(name="vo")
    chain = build_audio_track_chain(track, _project(), MarkerSet())
    assert chain.serialize() == "volume=volume='pow(10,(0.000000)/20)':eval=frame:precision=float"
