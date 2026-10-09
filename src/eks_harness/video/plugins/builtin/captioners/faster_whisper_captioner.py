"""faster-whisper backed captioner.

Lazy-imports ``faster_whisper`` so this module is safe to import on a
machine without the ``ml`` extras. Uses ``large-v3-turbo`` by default,
``device="auto"`` and ``compute_type="auto"`` so the same code runs on CPU
and GPU without explicit configuration.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.plugins.base import Captioner

if TYPE_CHECKING:
    from eks_harness.video.plugins.builtin.captioners import WordToken

__all__ = ["FasterWhisperCaptioner"]

_LOG = logging.getLogger(__name__)
_DEFAULT_MODEL = "large-v3-turbo"


class FasterWhisperCaptioner(Captioner):
    """Captioner backed by ``faster_whisper.WhisperModel``."""

    name: ClassVar[str] = "faster-whisper"

    def __init__(self) -> None:
        self._model: Any | None = None
        self._model_name: str | None = None

    def transcribe(self, audio_path: Path, opts: dict[str, Any] | None = None) -> list[WordToken]:
        from eks_harness.video.plugins.builtin.captioners import WordToken

        opts = opts or {}
        model_name = str(opts.get("model", _DEFAULT_MODEL))
        language = opts.get("language")
        beam_size = int(opts.get("beam_size", 5))

        model = self._get_model(model_name, opts)
        segments, _info = model.transcribe(
            str(audio_path),
            beam_size=beam_size,
            language=language,
            word_timestamps=True,
        )

        out: list[WordToken] = []
        for segment in segments:
            words = getattr(segment, "words", None) or []
            for word in words:
                text = (getattr(word, "word", "") or "").strip()
                if not text:
                    continue
                start = float(getattr(word, "start", 0.0) or 0.0)
                end = float(getattr(word, "end", start) or start)
                probability = getattr(word, "probability", None)
                confidence = float(probability) if probability is not None else None
                out.append(WordToken(text=text, start=start, end=end, confidence=confidence))
        return out

    def _get_model(self, model_name: str, opts: dict[str, Any]) -> Any:
        if self._model is not None and self._model_name == model_name:
            return self._model

        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed; install it with "
                "`pip install eks-harness[ml]` to enable speech-to-text captions"
            ) from exc

        device = str(opts.get("device", "auto"))
        compute_type = str(opts.get("compute_type", "auto"))
        _LOG.info("loading faster-whisper model %s (device=%s)", model_name, device)
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._model_name = model_name
        return self._model
