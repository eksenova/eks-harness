"""Per-target encoding presets (TikTok / Reels / Shorts / archival / experimental)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PRESETS", "EncodingPreset"]


@dataclass(frozen=True)
class EncodingPreset:
    """Resolution + codec + bitrate envelope for a specific delivery target."""

    codec: str
    width: int
    height: int
    fps: int
    bitrate: str
    maxrate: str
    bufsize: str
    profile: str
    pixfmt: str
    color: str
    audio_codec: str
    audio_bitrate: str
    movflags: str
    tag: str | None = None


PRESETS: dict[str, EncodingPreset] = {
    "tiktok-1080-h264": EncodingPreset(
        codec="h264", width=1080, height=1920, fps=30,
        bitrate="10M", maxrate="12M", bufsize="20M",
        profile="high", pixfmt="yuv420p", color="bt709",
        audio_codec="aac", audio_bitrate="192k", movflags="+faststart",
    ),
    "reels-1080-h264": EncodingPreset(
        codec="h264", width=1080, height=1920, fps=30,
        bitrate="9M", maxrate="11M", bufsize="18M",
        profile="high", pixfmt="yuv420p", color="bt709",
        audio_codec="aac", audio_bitrate="192k", movflags="+faststart",
    ),
    "shorts-1080-h264": EncodingPreset(
        codec="h264", width=1080, height=1920, fps=30,
        bitrate="12M", maxrate="14M", bufsize="24M",
        profile="high", pixfmt="yuv420p", color="bt709",
        audio_codec="aac", audio_bitrate="192k", movflags="+faststart",
    ),
    "archival-h265": EncodingPreset(
        codec="hevc", width=3840, height=2160, fps=60,
        bitrate="40M", maxrate="60M", bufsize="80M",
        profile="main10", pixfmt="yuv420p10le", color="bt2020",
        audio_codec="aac", audio_bitrate="320k", movflags="+faststart",
        tag="hvc1",
    ),
    "av1-experimental": EncodingPreset(
        codec="av1", width=1920, height=1080, fps=30,
        bitrate="6M", maxrate="8M", bufsize="12M",
        profile="main", pixfmt="yuv420p", color="bt709",
        audio_codec="aac", audio_bitrate="192k", movflags="+faststart",
    ),
}
