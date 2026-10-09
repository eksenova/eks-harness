from __future__ import annotations

from pathlib import Path

from eks_harness.paths import resolve_paths


def state_dir() -> Path:
    path = resolve_paths().state_dir / "studio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = resolve_paths().cache_dir / "studio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def preview_cache_dir() -> Path:
    return cache_dir() / "preview_cache"
