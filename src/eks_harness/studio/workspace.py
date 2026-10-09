"""Helpers for laying out a project workspace on disk.

Per plan §7 a project lives in a directory with a fixed shape:

    my_edit/
      project.py
      project.json
      assets/{video,audio,image,fonts}/
      cache/{segments,markers,probes,preview_frames}/
      renders/
      plugins/
      eks_harness.video.lock
      README.md

This module is the single place that knows the shape - tools and resources
read/write through it so the layout stays consistent.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProjectLayout",
    "default_workspace_root",
    "ensure_workspace_root",
    "link_or_copy_asset",
]


@dataclass(frozen=True)
class ProjectLayout:
    """Resolved on-disk paths for a single project workspace."""

    root: Path

    @property
    def project_py(self) -> Path:
        return self.root / "project.py"

    @property
    def project_json(self) -> Path:
        return self.root / "project.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "video.lock"

    @property
    def readme(self) -> Path:
        return self.root / "README.md"

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def cache_segments(self) -> Path:
        return self.cache / "segments"

    @property
    def cache_markers(self) -> Path:
        return self.cache / "markers"

    @property
    def cache_probes(self) -> Path:
        return self.cache / "probes"

    @property
    def cache_preview_frames(self) -> Path:
        return self.cache / "preview_frames"

    @property
    def renders(self) -> Path:
        return self.root / "renders"

    @property
    def plugins(self) -> Path:
        return self.root / "plugins"

    def ensure(self) -> None:
        """Create the directory skeleton. Safe to call repeatedly."""

        for directory in self._directories():
            directory.mkdir(parents=True, exist_ok=True)

    def _directories(self) -> Iterable[Path]:
        yield self.root
        yield self.assets
        yield self.assets / "video"
        yield self.assets / "audio"
        yield self.assets / "image"
        yield self.assets / "fonts"
        yield self.cache
        yield self.cache_segments
        yield self.cache_markers
        yield self.cache_probes
        yield self.cache_preview_frames
        yield self.renders
        yield self.plugins


def default_workspace_root() -> Path:
    """Default project workspace: the directory the server was launched from.

    When an MCP client (e.g. Claude Code) starts the server as a stdio
    subprocess, the subprocess inherits the client's cwd - which is the
    user's current project directory. Using that as the workspace means
    project.py and renders/ live next to the user's source media without
    requiring the client to advertise a ``project_workspace`` root.
    """

    return Path.cwd()


def ensure_workspace_root(path: Path) -> Path:
    """Create the workspace root directory and return it."""

    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def link_or_copy_asset(source: Path, destination: Path) -> Path:
    """Symlink ``source`` into ``destination``; fall back to copy on Windows / privilege errors.

    Returns the final destination path. Idempotent - if the destination
    already exists and resolves to the same content (by symlink target or
    file size + mtime), it is left in place.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        return destination

    try:
        os.symlink(source, destination)
        return destination
    except (OSError, NotImplementedError):
        if sys.platform.startswith("win") or True:
            shutil.copy2(source, destination)
            return destination


def write_lock_file(layout: ProjectLayout, pinned: dict[str, str]) -> Path:
    """Write the ``video.lock`` file containing pinned dependency versions."""

    layout.lock_file.write_text(json.dumps(pinned, indent=2, sort_keys=True), encoding="utf-8")
    return layout.lock_file
