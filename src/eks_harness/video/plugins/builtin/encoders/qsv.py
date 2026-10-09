"""Intel QuickSync (QSV) encoder plugin."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.plugins.base import Encoder

if TYPE_CHECKING:
    from eks_harness.video.ir.render_settings import RenderSettings

__all__ = ["QsvEncoder"]

_SUPPORTED_CODECS: dict[str, str] = {
    "h264": "h264_qsv",
    "hevc": "hevc_qsv",
    "av1": "av1_qsv",
}


class QsvEncoder(Encoder):
    name: ClassVar[str] = "qsv"

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
        return 80 if "_qsv" in result.stdout else 0

    def output_args(self, settings: RenderSettings) -> list[str]:
        codec_name = _resolve_codec(settings.preset)
        ffmpeg_codec = _SUPPORTED_CODECS[codec_name]
        args: list[str] = ["-c:v", ffmpeg_codec, "-preset", "veryfast"]
        if settings.bitrate_kbps is not None:
            args.extend(["-b:v", f"{settings.bitrate_kbps}k"])
        else:
            args.extend(["-global_quality", "23"])
        return args


def _resolve_codec(preset_name: str) -> str:
    from .presets import PRESETS

    preset = PRESETS.get(preset_name)
    if preset is None or preset.codec not in _SUPPORTED_CODECS:
        return "h264"
    return preset.codec
