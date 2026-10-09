"""Common helper for the audio/video/image library resources.

Each library category is a thin filter over the granted ``media_library``
root: list files of the relevant kind, return JSON metadata for one file by
relative path. Heavy probes are cached on disk by the SDK; here we keep
things cheap.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..roots import RootsManager

__all__ = ["AUDIO_EXT", "IMAGE_EXT", "VIDEO_EXT", "describe_media"]

VIDEO_EXT = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"})
AUDIO_EXT = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"})
IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"})


def describe_media(
    *,
    roots: RootsManager,
    rel_path: str,
    extensions: Iterable[str],
    category: str,
) -> str:
    media_root = roots.media_library()
    if media_root is None:
        raise PermissionError("no media_library root has been granted")

    if rel_path in {"", "/", "."}:
        return _list_directory(media_root, extensions=extensions, category=category)

    target = (media_root / rel_path.lstrip("/")).resolve()
    try:
        target.relative_to(media_root)
    except ValueError as exc:
        raise PermissionError(f"path {rel_path} escapes the media_library root") from exc

    if not target.exists():
        raise FileNotFoundError(f"no such {category} file: {rel_path}")

    if target.is_dir():
        return _list_directory(target, extensions=extensions, category=category)

    return json.dumps(_describe_file(target, media_root), indent=2, sort_keys=True)


def _list_directory(directory: Path, *, extensions: Iterable[str], category: str) -> str:
    ext_set = {e.lower() for e in extensions}
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ext_set:
            continue
        try:
            relative = path.relative_to(directory)
        except ValueError:
            continue
        entries.append(
            {
                "path": str(relative).replace("\\", "/"),
                "size_bytes": path.stat().st_size,
            }
        )
    return json.dumps({"category": category, "entries": entries}, indent=2, sort_keys=True)


def _describe_file(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "suffix": path.suffix.lower(),
        "absolute_uri": path.as_uri(),
    }
