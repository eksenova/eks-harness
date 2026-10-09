"""WinML / ONNX Whisper captioner - GPU/NPU-accelerated speech-to-text.

``faster-whisper`` (CTranslate2) has no AMD-GPU path on Windows, so STT runs
on the CPU there - the slowest ML step in the caption pipeline. This backend
runs an ONNX Whisper model through ONNX Runtime with the AMD **MIGraphX**
execution provider (the AMD GPU) when it's available, falling back to CPU
otherwise. The encoder/decoder sessions are routed onto the GPU via
:func:`eks_harness.video.render.winml_ep.install_autoep_session_hook` (MIGraphX is a
plugin EP and can't be named in optimum's legacy ``provider=`` string). The
one-time graph compile is cached to disk; steady-state inference is ~4x CPU.

Uses a pre-exported ONNX Whisper repo (``onnx-community/whisper-base``;
optimum's on-the-fly export of Whisper seq2seq hangs). The ONNX graph can't
do DTW cross-attention alignment, so per-word timing is approximated by
distributing each ASR segment's window across its words by length.

Requires ``pip install optimum optimum-onnx`` (plus the WinML packages for
GPU; CPU works without them). Lazy-imported so a stock install stays
import-safe.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.plugins.base import Captioner

if TYPE_CHECKING:
    from eks_harness.video.plugins.builtin.captioners import WordToken

__all__ = ["WinmlWhisperCaptioner"]

_LOG = logging.getLogger(__name__)
# Pre-exported ONNX Whisper (encoder_model.onnx + decoder_model_merged.onnx).
# optimum's on-the-fly `export=True` for Whisper seq2seq hangs on recent
# optimum/transformers, so we use a repo that ships the ONNX graphs directly.
_DEFAULT_MODEL = "onnx-community/whisper-base"


class WinmlWhisperCaptioner(Captioner):
    """ONNX Whisper captioner accelerated via Windows ML EPs (AMD GPU/NPU)."""

    name: ClassVar[str] = "winml-whisper"

    def __init__(self) -> None:
        self._pipeline: Any | None = None
        self._model_name: str | None = None
        self._provider: str | None = None

    def register(self) -> bool:
        # ONNX Runtime EP acceleration is a Windows feature; on other
        # platforms this backend offers no advantage over faster-whisper, so
        # keep it Windows-only to avoid shadowing the default there.
        return platform.system() == "Windows"

    def transcribe(self, audio_path: Path, opts: dict[str, Any] | None = None) -> list[WordToken]:

        opts = opts or {}
        model_name = str(opts.get("model", _DEFAULT_MODEL))
        language = opts.get("language")

        asr = self._get_pipeline(model_name)

        import librosa

        audio, _sr = librosa.load(str(audio_path), sr=16000, mono=True)
        # ONNX-exported Whisper can't produce true word-level timestamps -
        # that needs DTW over cross-attention weights, which the ONNX graph
        # doesn't expose (transformers' word path crashes on the ORT model).
        # We request SEGMENT-level timestamps and distribute words within each
        # segment proportionally to their length. Good enough for on-screen
        # captions, which group words anyway.
        call_kwargs: dict[str, Any] = {"return_timestamps": True}
        if language:
            call_kwargs["generate_kwargs"] = {"language": language}
        result = asr({"raw": audio, "sampling_rate": 16000}, **call_kwargs)

        out: list[WordToken] = []
        for chunk in result.get("chunks", []):
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            ts = chunk.get("timestamp") or (None, None)
            seg_start = float(ts[0]) if ts[0] is not None else 0.0
            seg_end = float(ts[1]) if ts[1] is not None else seg_start
            out.extend(_distribute_words(text, seg_start, seg_end))
        return out

    def _get_pipeline(self, model_name: str) -> Any:
        if self._pipeline is not None and self._model_name == model_name:
            return self._pipeline

        try:
            from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
            from transformers import AutoProcessor, pipeline
        except ImportError as exc:
            raise RuntimeError(
                "winml-whisper requires `pip install optimum optimum-onnx` "
                "(and the Windows ML packages for GPU acceleration)"
            ) from exc

        from eks_harness.video.render.winml_ep import install_autoep_session_hook

        # MIGraphX is a plugin EP - it can't be named in optimum's legacy
        # `provider=` string. Install the autoEP hook so the encoder/decoder
        # InferenceSessions optimum builds get routed onto the AMD GPU; pass
        # the CPU provider so optimum's provider validation passes (the hook
        # upgrades the session, or cleanly leaves it on CPU when no GPU EP).
        gpu = install_autoep_session_hook()
        provider = "CPUExecutionProvider"
        _LOG.info(
            "loading ONNX Whisper %s (autoEP GPU=%s, base provider=%s)",
            model_name,
            gpu,
            provider,
        )
        model = ORTModelForSpeechSeq2Seq.from_pretrained(
            model_name,
            encoder_file_name="encoder_model.onnx",
            decoder_file_name="decoder_model_merged.onnx",
            provider=provider,
        )
        processor = AutoProcessor.from_pretrained(model_name)
        # device=-1 keeps the transformers pipeline from trying to move the
        # ORT model onto a torch CUDA device - the ONNX Runtime *provider*
        # (set above) controls GPU placement. Without this, ROCm torch makes
        # the pipeline request a non-existent CUDAExecutionProvider.
        asr = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            device=-1,
        )
        self._pipeline = asr
        self._model_name = model_name
        self._provider = provider
        return asr


def _distribute_words(text: str, seg_start: float, seg_end: float) -> list["WordToken"]:
    """Split a segment's text into words with timing proportional to length.

    ONNX Whisper gives segment-level ``[start, end]`` only; we apportion that
    window across the segment's words weighted by character count so longer
    words get more time. The boundaries are monotonic and exactly fill the
    segment, which is what caption rendering needs.
    """

    from eks_harness.video.plugins.builtin.captioners import WordToken

    words = text.split()
    if not words:
        return []
    span = max(0.0, seg_end - seg_start)
    weights = [max(1, len(w)) for w in words]
    total = sum(weights)
    out: list[WordToken] = []
    cursor = seg_start
    for word, weight in zip(words, weights):
        dur = span * (weight / total)
        start = cursor
        end = seg_start + span if word is words[-1] else cursor + dur
        out.append(WordToken(text=word, start=round(start, 3), end=round(end, 3)))
        cursor = end
    return out
