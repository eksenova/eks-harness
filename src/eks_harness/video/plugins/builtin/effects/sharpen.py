"""Sharpen effect plugin (ffmpeg ``unsharp``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import Sharpen
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["SharpenPlugin"]


class SharpenPlugin(Effect):
    name: ClassVar[str] = "sharpen"
    model: ClassVar[type] = Sharpen
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Sharpen, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        expr = animated_to_ffmpeg_expr(ir.amount, ctx.project, ctx.markers)
        if expr is None:
            raise ValueError("sharpen: amount could not be lowered to ffmpeg expression")
        chain = FilterChain()
        chain.add(
            FilterNode(
                name="unsharp",
                params={
                    "luma_msize_x": 5,
                    "luma_msize_y": 5,
                    "luma_amount": expr,
                },
            )
        )
        return chain
