"""Content-addressable segment cache.

The cache keys segments by a sha256 of the canonical IR, declared
dependencies and the pluggy-resolved render-settings subset. Diskcache
stores the metadata; rendered segment files live alongside under
``segments/<key>.mp4``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import diskcache  # type: ignore[import-untyped]

from eks_harness.video import __version__ as _ENGINE_VERSION
from eks_harness.video.ir.tracks import Segment

__all__ = ["SegmentCache", "compute_segment_key"]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_segment_key(
    segment: Segment,
    *,
    media_digest: str,
    plugin_versions: dict[str, str] | None = None,
    render_settings: dict[str, Any] | None = None,
) -> str:
    """Compute the cache key for ``segment`` per the plan.

    Inputs are folded into a canonical JSON envelope and hashed with sha256.
    Mutating any field of the segment, the media digest, the plugin
    version-set or the render settings produces a different key.
    """

    envelope = {
        "engine": ".".join(_ENGINE_VERSION.split(".")[:2]),
        "segment": json.loads(segment.model_dump_json(by_alias=True)),
        "media": media_digest,
        "plugins": dict(sorted((plugin_versions or {}).items())),
        "render": dict(sorted((render_settings or {}).items())),
    }
    payload = _canonical_json(envelope).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SegmentCache:
    """Filesystem-backed segment cache."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.segments_dir = self.root / "segments"
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        self._meta = diskcache.Cache(directory=str(self.root / "meta"))

    def key_for(
        self,
        segment: Segment,
        *,
        media_digest: str,
        plugin_versions: dict[str, str] | None = None,
        render_settings: dict[str, Any] | None = None,
    ) -> str:
        return compute_segment_key(
            segment,
            media_digest=media_digest,
            plugin_versions=plugin_versions,
            render_settings=render_settings,
        )

    def lookup(self, key: str) -> Path | None:
        path = self.segments_dir / f"{key}.mp4"
        if path.exists() and key in self._meta:
            return path
        return None

    def store(self, key: str, src_path: Path) -> Path:
        target = self.segments_dir / f"{key}.mp4"
        if Path(src_path).resolve() != target.resolve():
            shutil.copyfile(src_path, target)
        self._meta[key] = {"size": target.stat().st_size}
        return target

    def evict(self, predicate: Callable[[str], bool]) -> int:
        removed = 0
        for key in list(self._meta.iterkeys()):
            if predicate(key):
                self._drop(key)
                removed += 1
        return removed

    def keys(self) -> Iterable[str]:
        return list(self._meta.iterkeys())

    def close(self) -> None:
        self._meta.close()

    def _drop(self, key: str) -> None:
        path = self.segments_dir / f"{key}.mp4"
        if path.exists():
            path.unlink()
        if key in self._meta:
            del self._meta[key]
