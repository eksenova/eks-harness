"""Built-in captioner (speech-to-text) plugins.

Each backend lazy-imports its underlying ML library so this package is
import-safe on a stock install. ``select_captioner`` is the high-level
entry point used by :class:`STTMarkersExtractor` and any caller that wants
"the best available backend on this host".
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

from eks_harness.video.plugins.base import Captioner

from .faster_whisper_captioner import FasterWhisperCaptioner
from .mlx_whisper_captioner import MlxWhisperCaptioner
from .whisperx_captioner import WhisperXCaptioner
from .winml_whisper_captioner import WinmlWhisperCaptioner

__all__ = [
    "FasterWhisperCaptioner",
    "MlxWhisperCaptioner",
    "WhisperXCaptioner",
    "WinmlWhisperCaptioner",
    "WordToken",
    "select_captioner",
]


@dataclass(frozen=True)
class WordToken:
    """One word emitted by a captioner with start/end offsets in seconds."""

    text: str
    start: float
    end: float
    confidence: float | None = None


def select_captioner(prefer: str | None = None) -> Captioner:
    """Resolve a usable captioner instance.

    When ``prefer`` is given, the named backend is returned from the registry
    or a :class:`RuntimeError` is raised. Otherwise the first backend that
    actually opted into registration is returned, with mlx-whisper preferred
    on Apple Silicon, then faster-whisper.
    """

    from eks_harness.video.plugins.registry import get_captioner, list_captioners

    if prefer is not None:
        plugin = get_captioner(prefer)
        if plugin is None:
            raise RuntimeError(
                f"captioner {prefer!r} is not registered; "
                f"available backends: {list_captioners() or '<none>'}"
            )
        return plugin

    preferred_order: list[str] = []
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        preferred_order.append("mlx-whisper")
    elif platform.system() == "Windows":
        # ONNX Whisper via Windows ML EPs (AMD GPU / NPU) beats CTranslate2,
        # which is CPU-only on Windows AMD. Registers only on Windows.
        preferred_order.append("winml-whisper")
    preferred_order.extend(["faster-whisper", "whisperx"])

    for name in preferred_order:
        plugin = get_captioner(name)
        if plugin is not None:
            return plugin

    raise RuntimeError(
        "no captioner backend available; install one with `pip install eks-harness[ml]` "
        "(faster-whisper) or provide a custom backend via the eks_harness.video.captioners "
        "entry point"
    )
