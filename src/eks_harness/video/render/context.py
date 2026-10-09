"""Render context and options passed through the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project
    from eks_harness.video.render.progress import ProgressReporter


@dataclass
class RenderOptions:
    """Caller-provided knobs for a single render."""

    output: Path
    mode: str = "final"
    range_start: float | None = None
    range_end: float | None = None
    cache_dir: Path | None = None
    workspace: Path | None = None
    ffmpeg_binary: str = "ffmpeg"
    progress_reporter: "ProgressReporter | None" = None


@dataclass
class RenderContext:
    """Per-render context shared across orchestrator stages and plugins."""

    project: Project
    options: RenderOptions
    markers: MarkerSet
    workspace: Path
    cache_dir: Path
    extra: dict[str, object] = field(default_factory=dict)


__all__ = ["RenderContext", "RenderOptions"]
