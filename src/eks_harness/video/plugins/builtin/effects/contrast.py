"""Contrast effect plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import Contrast
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, eq

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class ContrastPlugin(Effect):
    name: ClassVar[str] = "contrast"
    model: ClassVar[type] = Contrast
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Contrast, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        expr = animated_to_ffmpeg_expr(ir.amount, ctx.project, ctx.markers)
        if expr is None:
            raise ValueError("contrast: amount could not be lowered to ffmpeg expression")
        chain = FilterChain()
        chain.add(eq(contrast=expr))
        return chain
