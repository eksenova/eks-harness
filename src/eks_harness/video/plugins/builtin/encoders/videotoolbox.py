"""Apple VideoToolbox encoder plugin (macOS only)."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.plugins.base import Encoder

if TYPE_CHECKING:
    from eks_harness.video.ir.render_settings import RenderSettings

__all__ = ["VideotoolboxEncoder"]

_SUPPORTED_CODECS: dict[str, str] = {
    "h264": "h264_videotoolbox",
    "hevc": "hevc_videotoolbox",
}


class VideotoolboxEncoder(Encoder):
    name: ClassVar[str] = "videotoolbox"

    def probe(self) -> int:
        return 90 if platform.system() == "Darwin" else 0

    def output_args(self, settings: RenderSettings) -> list[str]:
        codec_name = _resolve_codec(settings.preset)
        ffmpeg_codec = _SUPPORTED_CODECS[codec_name]
        bitrate = settings.bitrate_kbps if settings.bitrate_kbps is not None else 10_000
        return ["-c:v", ffmpeg_codec, "-b:v", f"{bitrate}k"]


def _resolve_codec(preset_name: str) -> str:
    from .presets import PRESETS

    preset = PRESETS.get(preset_name)
    if preset is None or preset.codec not in _SUPPORTED_CODECS:
        return "h264"
    return preset.codec
