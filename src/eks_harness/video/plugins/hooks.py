"""Pluggy hookspecs / hookimpl markers for the video render lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pluggy

HOOK_NAMESPACE = "eks_harness.video"

hookspec = pluggy.HookspecMarker(HOOK_NAMESPACE)
hookimpl = pluggy.HookimplMarker(HOOK_NAMESPACE)


if TYPE_CHECKING:
    from eks_harness.video.ir import Project
    from eks_harness.video.ir.tracks import Segment
    from eks_harness.video.render.context import RenderContext


class VideoHookSpec:
    """Lifecycle hook specifications.

    Plugin packages declare ``@hookimpl`` methods matching these signatures
    and register through pluggy. ``cache_lookup`` is ``firstresult=True`` -
    the first plugin to return a non-``None`` value short-circuits the rest.
    """

    @hookspec
    def before_render(self, project: Project, ctx: RenderContext) -> None: ...

    @hookspec
    def after_segment_render(
        self,
        segment: Segment,
        output: Path,
        ctx: RenderContext,
    ) -> None: ...

    @hookspec
    def before_encode(self, args: list[str], ctx: RenderContext) -> list[str] | None: ...

    @hookspec
    def progress(self, event: dict[str, Any]) -> None: ...

    @hookspec(firstresult=True)
    def cache_lookup(self, key: str) -> Path | None: ...


__all__ = ["HOOK_NAMESPACE", "VideoHookSpec", "hookimpl", "hookspec"]
