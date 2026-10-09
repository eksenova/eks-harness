"""Built-in encoder plugins and the cross-platform negotiation helper."""

from __future__ import annotations

from dataclasses import dataclass, field

from eks_harness.video.plugins.base import Encoder

from .libx264 import Libx264Encoder
from .nvenc import NvencEncoder
from .presets import PRESETS, EncodingPreset
from .qsv import QsvEncoder
from .svtav1 import SvtAv1Encoder
from .videotoolbox import VideotoolboxEncoder

__all__ = [
    "PRESETS",
    "EncodeOptions",
    "EncodingPreset",
    "HardwareReport",
    "Libx264Encoder",
    "NvencEncoder",
    "QsvEncoder",
    "SvtAv1Encoder",
    "VideotoolboxEncoder",
    "select_encoder",
]


@dataclass(frozen=True)
class HardwareReport:
    """Coarse description of the host's video encode capabilities."""

    platform: str
    has_nvenc: bool = False
    has_qsv: bool = False
    has_videotoolbox: bool = False
    ffmpeg_encoders: tuple[str, ...] = ()


@dataclass
class EncodeOptions:
    """Negotiated ffmpeg args for a chosen encoder."""

    encoder_name: str
    codec: str
    args: list[str] = field(default_factory=list)


def select_encoder(report: HardwareReport, prefer: str | None = None) -> Encoder:
    """Return the highest-priority encoder for the given hardware report.

    Order: explicit ``prefer`` -> platform-native HW -> libx264 fallback.
    """

    candidates = _candidates_for(report)
    if prefer is not None:
        for candidate in candidates:
            if candidate.name == prefer:
                return candidate
        raise LookupError(f"requested encoder {prefer!r} is not available on this host")
    return candidates[0]


def _candidates_for(report: HardwareReport) -> list[Encoder]:
    ordered: list[Encoder] = []
    if report.platform == "Darwin" and report.has_videotoolbox:
        ordered.append(VideotoolboxEncoder())
    if report.has_nvenc:
        ordered.append(NvencEncoder())
    if report.has_qsv:
        ordered.append(QsvEncoder())
    ordered.append(Libx264Encoder())
    ordered.append(SvtAv1Encoder())
    return ordered
