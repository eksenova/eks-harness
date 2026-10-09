"""Crop effect plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import Crop
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class CropPlugin(Effect):
    name: ClassVar[str] = "crop"
    model: ClassVar[type] = Crop
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Crop, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        w = animated_to_ffmpeg_expr(ir.w, ctx.project, ctx.markers)
        h = animated_to_ffmpeg_expr(ir.h, ctx.project, ctx.markers)
        x = animated_to_ffmpeg_expr(ir.x, ctx.project, ctx.markers)
        y = animated_to_ffmpeg_expr(ir.y, ctx.project, ctx.markers)
        if any(v is None for v in (w, h, x, y)):
            raise ValueError("crop: parameters could not be lowered to ffmpeg expressions")
        chain = FilterChain()
        chain.add(FilterNode(name="crop", positional=[w, h, x, y]))
        return chain
