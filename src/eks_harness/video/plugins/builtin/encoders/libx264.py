"""libx264 software encoder - always-available fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.plugins.base import Encoder

if TYPE_CHECKING:
    from eks_harness.video.ir.render_settings import RenderSettings

__all__ = ["Libx264Encoder"]


class Libx264Encoder(Encoder):
    name: ClassVar[str] = "libx264"

    def probe(self) -> int:
        return 10

    def output_args(self, settings: RenderSettings) -> list[str]:
        crf = settings.crf if settings.crf is not None else 19
        args: list[str] = ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf)]
        if settings.bitrate_kbps is not None:
            args.extend(["-b:v", f"{settings.bitrate_kbps}k"])
        return args
