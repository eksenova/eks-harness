"""mlx-whisper backed captioner (Apple Silicon only).

The class is importable on every platform so entry-point discovery does not
fail on non-Apple hosts. It opts out of registration on anything that is not
``Darwin/arm64`` with ``mlx_whisper`` actually importable; calls to
:meth:`transcribe` on an unsupported host raise ``NotImplementedError``.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.plugins.base import Captioner

if TYPE_CHECKING:
    from eks_harness.video.plugins.builtin.captioners import WordToken

__all__ = ["MlxWhisperCaptioner"]

_LOG = logging.getLogger(__name__)
_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


class MlxWhisperCaptioner(Captioner):
    """Captioner backed by Apple's MLX whisper implementation."""

    name: ClassVar[str] = "mlx-whisper"

    def register(self) -> bool:
        if not _is_apple_silicon():
            _LOG.debug("mlx-whisper unavailable: host is not Darwin/arm64")
            return False
        try:
            import mlx_whisper  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            _LOG.debug("mlx-whisper unavailable: mlx_whisper package not installed")
            return False
        return True

    def transcribe(self, audio_path: Path, opts: dict[str, Any] | None = None) -> list[WordToken]:
        from eks_harness.video.plugins.builtin.captioners import WordToken

        if not _is_apple_silicon():
            raise NotImplementedError(
                "mlx-whisper requires Apple Silicon (Darwin/arm64); "
                "use 'faster-whisper' or 'whisperx' instead"
            )

        try:
            import mlx_whisper
        except ImportError as exc:
            raise RuntimeError(
                "mlx-whisper is not installed; install it with `pip install mlx-whisper`"
            ) from exc

        opts = opts or {}
        model_name = str(opts.get("model", _DEFAULT_MODEL))
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=model_name,
            word_timestamps=True,
        )

        out: list[WordToken] = []
        for segment in result.get("segments", []):
            for word in segment.get("words", []) or []:
                text = str(word.get("word", "")).strip()
                if not text:
                    continue
                start = float(word.get("start", 0.0))
                end = float(word.get("end", start))
                probability = word.get("probability")
                confidence = float(probability) if probability is not None else None
                out.append(WordToken(text=text, start=start, end=end, confidence=confidence))
        return out


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"
