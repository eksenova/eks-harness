"""AMD AMF hardware encoder plugin (h264_amf / hevc_amf / av1_amf)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.plugins.base import Encoder

if TYPE_CHECKING:
    from eks_harness.video.ir.render_settings import RenderSettings

__all__ = ["AmfEncoder"]

_SUPPORTED_CODECS: dict[str, str] = {
    "h264": "h264_amf",
    "hevc": "hevc_amf",
    "av1": "av1_amf",
}


class AmfEncoder(Encoder):
    name: ClassVar[str] = "amf"

    def probe(self) -> int:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return 0
        # Rank just below NVENC (100) so a discrete NVIDIA card wins if both
        # are somehow present, but well above the libx264 software fallback.
        return 95 if "h264_amf" in result.stdout else 0

    def output_args(self, settings: RenderSettings) -> list[str]:
        codec_name = _resolve_codec(settings.preset)
        ffmpeg_codec = _SUPPORTED_CODECS[codec_name]
        args: list[str] = ["-c:v", ffmpeg_codec, "-quality", "quality"]
        if settings.bitrate_kbps is not None:
            args.extend(["-rc", "vbr_latency", "-b:v", f"{settings.bitrate_kbps}k"])
        else:
            qp = settings.crf if settings.crf is not None else 22
            args.extend(["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp)])
        return args


def _resolve_codec(preset_name: str) -> str:
    from .presets import PRESETS

    preset = PRESETS.get(preset_name)
    if preset is None or preset.codec not in _SUPPORTED_CODECS:
        return "h264"
    return preset.codec
