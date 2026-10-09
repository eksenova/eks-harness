"""Speech-to-text marker extractor.

Delegates the actual transcription to a registered :class:`Captioner` (the
backend chosen by :attr:`STTMarkers.backend`) and projects the resulting
:class:`WordToken` list into three families of marker streams:

* ``word`` -- every word start time, in order
* ``word:<text>`` -- one stream per case-folded alphanumeric token, useful
  for ``WordRef`` lookups (e.g. cue an effect on every "drop")
* ``sentence`` -- start time of each sentence, derived from punctuation in
  word text and from inter-word gaps when punctuation is absent

Results are content-addressed by audio digest + backend name and cached on
disk through the same :class:`MarkerCache` used by the beat tracker.
"""

from __future__ import annotations

import logging
import re
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.compile.markers import MarkerSet, WordHit
from eks_harness.video.ir.markers import STTMarkers
from eks_harness.video.plugins.base import MarkerExtractor

from ._cache import MarkerCache, compute_audio_digest

if TYPE_CHECKING:
    from eks_harness.video.plugins.builtin.captioners import WordToken
    from eks_harness.video.render.context import RenderContext

__all__ = ["STTMarkersExtractor"]

_LOG = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"[^0-9a-z]+")
_SENTENCE_END_RE = re.compile(r"[.!?]['\")\]]*$")
_SENTENCE_GAP_SECONDS = 0.6


class STTMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``STTMarkers`` IR sources."""

    name: ClassVar[str] = "stt_markers"
    handles: ClassVar[str] = "stt_markers"

    def extract(self, source: STTMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        audio_path = self._resolve_audio_path(source, ctx)
        if audio_path is None or not audio_path.exists():
            raise RuntimeError(
                f"stt_markers: cannot resolve audio path for source={source.source!r}"
            )

        cache = self._open_cache(ctx)
        cache_key = self._cache_key(audio_path, source)

        words: list[WordToken] | None = None
        if cache is not None:
            cached = cache.lookup(cache_key)
            if cached is not None:
                words = _payload_to_words(cached)

        if words is None:
            captioner = self._select_captioner(source)
            words = list(captioner.transcribe(audio_path))
            if cache is not None:
                cache.store(cache_key, _words_to_payload(words))

        if cache is not None:
            cache.close()

        return _words_to_marker_set(source, words)

    def _resolve_audio_path(self, source: STTMarkers, ctx: RenderContext | None) -> Path | None:
        candidate = Path(source.source)
        if candidate.exists():
            return candidate
        if ctx is not None:
            ws_candidate = ctx.workspace / source.source
            if ws_candidate.exists():
                return ws_candidate
        return None

    def _open_cache(self, ctx: RenderContext | None) -> MarkerCache | None:
        if ctx is None or ctx.cache_dir is None:
            return None
        return MarkerCache(ctx.cache_dir)

    def _cache_key(self, audio_path: Path, source: STTMarkers) -> str:
        digest = compute_audio_digest(audio_path)
        return f"stt-{digest}-{source.backend}"

    def _select_captioner(self, source: STTMarkers) -> Any:
        from eks_harness.video.plugins.builtin.captioners import select_captioner

        try:
            return select_captioner(prefer=source.backend)
        except RuntimeError as exc:
            raise RuntimeError(
                f"stt_markers: backend {source.backend!r} is not available "
                f"({exc}); install it via `pip install eks-harness[ml]`"
            ) from exc


def _slug(text: str) -> str:
    return _TOKEN_RE.sub("", text.casefold())


def _words_to_payload(words: list[WordToken]) -> dict[str, Any]:
    return {
        "words": [
            {
                "text": word.text,
                "start": float(word.start),
                "end": float(word.end),
                "confidence": None if word.confidence is None else float(word.confidence),
            }
            for word in words
        ]
    }


def _payload_to_words(payload: dict[str, Any]) -> list[WordToken]:
    from eks_harness.video.plugins.builtin.captioners import WordToken

    raw = payload.get("words", []) if isinstance(payload, dict) else []
    out: list[WordToken] = []
    for entry in raw:
        confidence = entry.get("confidence")
        out.append(
            WordToken(
                text=str(entry["text"]),
                start=float(entry["start"]),
                end=float(entry["end"]),
                confidence=None if confidence is None else float(confidence),
            )
        )
    return out


def _sentence_starts(words: list[WordToken]) -> list[float]:
    if not words:
        return []
    starts: list[float] = [float(words[0].start)]
    for prev, current in pairwise(words):
        gap = float(current.start) - float(prev.end)
        ends_sentence = bool(_SENTENCE_END_RE.search(prev.text))
        if ends_sentence or gap >= _SENTENCE_GAP_SECONDS:
            starts.append(float(current.start))
    return starts


def _words_to_marker_set(source: STTMarkers, words: list[WordToken]) -> MarkerSet:
    out = MarkerSet()
    word_starts: list[float] = []
    per_token: dict[str, list[float]] = {}
    word_hits: list[WordHit] = []

    for word in words:
        start = float(word.start)
        end = float(word.end)
        word_starts.append(start)
        slug = _slug(word.text)
        if slug:
            per_token.setdefault(f"word:{slug}", []).append(start)
        word_hits.append(
            WordHit(text=word.text, t_start=start, t_end=end, source=source.name)
        )

    out.streams["word"] = sorted(word_starts)
    for stream_name, times in per_token.items():
        out.streams[stream_name] = sorted(times)
    out.streams["sentence"] = sorted(_sentence_starts(words))
    out.words.extend(word_hits)
    out.named[source.name] = sorted(word_starts)
    return out
