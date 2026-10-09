"""Marker cache backed by diskcache, keyed by deterministic content hashes.

Sibling of :class:`eks_harness.video.render.cache.SegmentCache` - same diskcache
backend, different on-disk layout (``markers/<key>.json``) so beat / STT /
scene results survive across runs without colliding with segment caches.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import diskcache  # type: ignore[import-untyped]

__all__ = ["MarkerCache", "compute_audio_digest"]


def compute_audio_digest(path: Path) -> str:
    """Content hash for an audio/video source file used as a cache key prefix."""

    h = hashlib.sha256()
    if not path.exists():
        return f"missing:{path}"
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class MarkerCache:
    """Filesystem-backed cache for marker extraction results."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.markers_dir = self.root / "markers"
        self.markers_dir.mkdir(parents=True, exist_ok=True)
        self._meta = diskcache.Cache(directory=str(self.root / "markers_meta"))

    def lookup(self, key: str) -> dict[str, Any] | None:
        path = self.markers_dir / f"{key}.json"
        if path.exists() and key in self._meta:
            try:
                payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                return payload
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def store(self, key: str, payload: dict[str, Any]) -> Path:
        path = self.markers_dir / f"{key}.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        self._meta[key] = {"size": path.stat().st_size}
        return path

    def close(self) -> None:
        self._meta.close()
