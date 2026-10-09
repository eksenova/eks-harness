"""Pan effect plugin.

Pan emits a ``crop`` filter that holds the source dimensions while shifting
the centre by ``(dx, dy)`` pixels. Animated offsets are lowered to ffmpeg
expressions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import Pan
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class PanPlugin(Effect):
    name: ClassVar[str] = "pan"
    model: ClassVar[type] = Pan
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Pan, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        dx = animated_to_ffmpeg_expr(ir.dx, ctx.project, ctx.markers)
        dy = animated_to_ffmpeg_expr(ir.dy, ctx.project, ctx.markers)
        if dx is None or dy is None:
            raise ValueError("pan: offsets could not be lowered to ffmpeg expressions")
        chain = FilterChain()
        chain.add(
            FilterNode(
                name="crop",
                positional=["iw", "ih", f"({dx})", f"({dy})"],
            )
        )
        return chain
