"""Freeze effect plugin (ffmpeg ``tpad``).

The IR's ``at`` field declares when to start the freeze (a ``TimeRef``),
and ``hold`` the duration of the frozen tail in seconds. The current
implementation emits ``tpad=stop_mode=clone:stop_duration=<hold>`` which
extends the clip past its natural end by repeating the last frame.

Mid-segment freezes are not yet supported. If ``at`` resolves to anything
other than the segment's end (or no ``segment`` is available to compare
against on the context), ``compile_graph`` raises ``NotImplementedError``
with a clear message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.effects import Freeze
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["FreezePlugin"]


class FreezePlugin(Effect):
    name: ClassVar[str] = "freeze"
    model: ClassVar[type] = Freeze
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Freeze, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        at_seconds = resolve_time(ir.at, ctx.project, ctx.markers)
        segment = getattr(ctx, "segment", None)
        if segment is not None:
            seg_end = resolve_time(segment.out, ctx.project, ctx.markers)
            if abs(at_seconds - seg_end) > 1e-3:
                raise NotImplementedError(
                    "in-segment Freeze not yet implemented; only segment-end tail "
                    f"extension is supported (got at={at_seconds:.3f}s, "
                    f"segment ends at {seg_end:.3f}s)"
                )
        chain = FilterChain()
        chain.add(
            FilterNode(
                name="tpad",
                params={"stop_mode": "clone", "stop_duration": f"{float(ir.hold):.6f}"},
            )
        )
        return chain
