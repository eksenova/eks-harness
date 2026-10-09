"""Scene-cut marker extractor backed by PySceneDetect.

Lazy-imports ``scenedetect`` so the SDK installs cleanly without it.
A missing dependency produces a single warning and an empty result rather
than aborting the render -- scene markers are typically optional cues.
Results are cached on disk by content hash + threshold.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.markers import SceneMarkers
from eks_harness.video.plugins.base import MarkerExtractor

from ._cache import MarkerCache, compute_audio_digest

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["SceneMarkersExtractor"]

_LOG = logging.getLogger(__name__)
_WARNED_NO_SCENEDETECT = False


class SceneMarkersExtractor(MarkerExtractor):
    """Marker extractor for ``SceneMarkers`` IR sources."""

    name: ClassVar[str] = "scene_markers"
    handles: ClassVar[str] = "scene_markers"

    def extract(self, source: SceneMarkers, ctx: RenderContext) -> MarkerSet:  # type: ignore[override]
        media_path = self._resolve_media_path(source, ctx)
        if media_path is None or not media_path.exists():
            _LOG.warning(
                "scene_markers: cannot resolve media for source=%r; emitting empty stream",
                source.source,
            )
            return _empty_marker_set(source)

        cache = self._open_cache(ctx)
        cache_key = f"scene-{compute_audio_digest(media_path)}-{source.threshold:.3f}"

        cuts: list[float] | None = None
        if cache is not None:
            cached = cache.lookup(cache_key)
            if isinstance(cached, dict):
                cuts = [float(t) for t in cached.get("scene", [])]

        if cuts is None:
            try:
                cuts = _detect_scenes(media_path, source.threshold)
            except _SceneDetectUnavailable:
                self._warn_missing_scenedetect()
                cuts = []
            except Exception:
                _LOG.exception(
                    "scenedetect failed for %s; emitting empty stream", media_path
                )
                cuts = []
            if cache is not None:
                cache.store(cache_key, {"scene": cuts})

        if cache is not None:
            cache.close()

        return _cuts_to_marker_set(source, cuts)

    def _resolve_media_path(self, source: SceneMarkers, ctx: RenderContext | None) -> Path | None:
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

    def _warn_missing_scenedetect(self) -> None:
        global _WARNED_NO_SCENEDETECT
        if _WARNED_NO_SCENEDETECT:
            return
        _LOG.warning(
            "scenedetect not installed; install via `pip install scenedetect[opencv]` "
            "to enable scene markers"
        )
        _WARNED_NO_SCENEDETECT = True


class _SceneDetectUnavailable(RuntimeError):
    pass


def _detect_scenes(media_path: Path, threshold: float) -> list[float]:
    try:
        from scenedetect import ContentDetector, detect  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _SceneDetectUnavailable from exc

    scenes: Any = detect(str(media_path), ContentDetector(threshold=float(threshold)))
    cuts: list[float] = []
    for entry in scenes:
        start = entry[0]
        try:
            cuts.append(float(start.get_seconds()))
        except AttributeError:
            cuts.append(float(start))
    return sorted({round(t, 9) for t in cuts})


def _empty_marker_set(source: SceneMarkers) -> MarkerSet:
    out = MarkerSet()
    out.streams["scene"] = []
    out.named[source.name] = []
    return out


def _cuts_to_marker_set(source: SceneMarkers, cuts: list[float]) -> MarkerSet:
    out = MarkerSet()
    sorted_cuts = sorted(float(t) for t in cuts)
    out.streams["scene"] = sorted_cuts
    out.named[source.name] = list(sorted_cuts)
    return out
