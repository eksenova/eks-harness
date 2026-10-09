"""Color grade effect plugin (LUT + exposure / temperature / tint)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import ColorGrade
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode, eq

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class ColorGradePlugin(Effect):
    name: ClassVar[str] = "color_grade"
    model: ClassVar[type] = ColorGrade
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: ColorGrade, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        chain = FilterChain()
        if ir.lut_path is not None:
            chain.add(FilterNode(name="lut3d", params={"file": str(ir.lut_path)}))

        exposure = animated_to_ffmpeg_expr(ir.exposure, ctx.project, ctx.markers)
        if exposure is None:
            raise ValueError("color_grade: exposure could not be lowered to ffmpeg expression")
        chain.add(eq(brightness=exposure))

        temperature = animated_to_ffmpeg_expr(ir.temperature, ctx.project, ctx.markers)
        tint = animated_to_ffmpeg_expr(ir.tint, ctx.project, ctx.markers)
        if temperature is None or tint is None:
            raise ValueError("color_grade: temperature/tint could not be lowered to ffmpeg expressions")
        chain.add(
            FilterNode(
                name="colorbalance",
                params={
                    "rs": temperature,
                    "bs": f"-({temperature})",
                    "gm": tint,
                },
            )
        )
        return chain
