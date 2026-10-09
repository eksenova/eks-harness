"""Gaussian blur effect plugin (ffmpeg ``gblur``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import Blur
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["BlurPlugin"]


class BlurPlugin(Effect):
    name: ClassVar[str] = "blur"
    model: ClassVar[type] = Blur
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Blur, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        expr = animated_to_ffmpeg_expr(ir.radius, ctx.project, ctx.markers)
        if expr is None:
            raise ValueError("blur: radius could not be lowered to ffmpeg expression")
        chain = FilterChain()
        chain.add(FilterNode(name="gblur", params={"sigma": expr}))
        return chain
