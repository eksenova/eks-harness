"""SVT-AV1 software encoder - high-quality AV1 software path."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.plugins.base import Encoder

if TYPE_CHECKING:
    from eks_harness.video.ir.render_settings import RenderSettings

__all__ = ["SvtAv1Encoder"]


class SvtAv1Encoder(Encoder):
    name: ClassVar[str] = "libsvtav1"

    def probe(self) -> int:
        # Lower than libx264 (10) so libx264 stays the default fallback.
        # Selectable explicitly via prefer= argument.
        return 5

    def output_args(self, settings: RenderSettings) -> list[str]:
        crf = settings.crf if settings.crf is not None else 30
        args: list[str] = ["-c:v", "libsvtav1", "-preset", "8", "-crf", str(crf)]
        if settings.bitrate_kbps is not None:
            args.extend(["-b:v", f"{settings.bitrate_kbps}k"])
        return args
