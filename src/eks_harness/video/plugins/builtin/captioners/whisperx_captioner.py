"""WhisperX backed captioner with forced wav2vec2 alignment.

WhisperX layers a phoneme-level alignment model on top of Whisper's word
timestamps, which produces noticeably tighter word boundaries than the raw
Whisper attention heads. The alignment model is loaded lazily per language.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.plugins.base import Captioner

if TYPE_CHECKING:
    from eks_harness.video.plugins.builtin.captioners import WordToken

__all__ = ["WhisperXCaptioner"]

_LOG = logging.getLogger(__name__)
_DEFAULT_MODEL = "large-v3"


class WhisperXCaptioner(Captioner):
    """Captioner backed by ``whisperx`` with wav2vec2 forced alignment."""

    name: ClassVar[str] = "whisperx"

    def __init__(self) -> None:
        self._model: Any | None = None
        self._model_name: str | None = None
        self._align_models: dict[str, tuple[Any, Any]] = {}

    def transcribe(self, audio_path: Path, opts: dict[str, Any] | None = None) -> list[WordToken]:
        from eks_harness.video.plugins.builtin.captioners import WordToken

        opts = opts or {}
        model_name = str(opts.get("model", _DEFAULT_MODEL))
        device = str(opts.get("device", "cuda"))
        compute_type = str(opts.get("compute_type", "float16"))
        batch_size = int(opts.get("batch_size", 16))

        whisperx = self._import_whisperx()
        model = self._get_model(whisperx, model_name, device, compute_type)

        audio = whisperx.load_audio(str(audio_path))
        result = model.transcribe(audio, batch_size=batch_size)
        language = result.get("language", opts.get("language", "en"))

        align_model, metadata = self._get_align_model(whisperx, language, device)
        aligned = whisperx.align(
            result["segments"],
            align_model,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )

        out: list[WordToken] = []
        for segment in aligned.get("segments", []):
            for word in segment.get("words", []):
                text = str(word.get("word", "")).strip()
                if not text:
                    continue
                start = float(word.get("start", 0.0))
                end = float(word.get("end", start))
                score = word.get("score")
                confidence = float(score) if score is not None else None
                out.append(WordToken(text=text, start=start, end=end, confidence=confidence))
        return out

    def _import_whisperx(self) -> Any:
        try:
            import whisperx  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "whisperx is not installed; install it with "
                "`pip install eks-harness[ml] whisperx` to enable forced-alignment captions"
            ) from exc
        return whisperx

    def _get_model(self, whisperx: Any, model_name: str, device: str, compute_type: str) -> Any:
        if self._model is not None and self._model_name == model_name:
            return self._model
        _LOG.info("loading whisperx model %s on %s", model_name, device)
        self._model = whisperx.load_model(model_name, device, compute_type=compute_type)
        self._model_name = model_name
        return self._model

    def _get_align_model(self, whisperx: Any, language: str, device: str) -> tuple[Any, Any]:
        cached = self._align_models.get(language)
        if cached is not None:
            return cached
        align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
        self._align_models[language] = (align_model, metadata)
        return align_model, metadata
