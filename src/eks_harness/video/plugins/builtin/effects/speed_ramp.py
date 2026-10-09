"""SpeedRamp effect plugin.

Emits ``setpts=PTS/<factor_expr>``. A factor > 1.0 speeds the clip up
(time compression); 0 < factor < 1.0 slows it down. The IR allows an
``Animated[float]`` factor; we only support factors that lower to an
ffmpeg expression - true variable-rate retiming with frame interpolation
or duplication would require orchestrator-level frame-rate adjustments
that don't exist yet.

If the factor doesn't fold, ``compile_graph`` raises
``NotImplementedError`` with a clear message. We deliberately do NOT
implement a frame-pipeline spill: the frame pipeline operates one input
frame at a time and can't change the frame count, so any "spill" would
silently produce wrong output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.ir.effects import SpeedRamp
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["SpeedRampPlugin"]


class SpeedRampPlugin(Effect):
    name: ClassVar[str] = "speed_ramp"
    model: ClassVar[type] = SpeedRamp
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: SpeedRamp, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        factor_expr = animated_to_ffmpeg_expr(ir.factor, ctx.project, ctx.markers)
        if factor_expr is None:
            raise NotImplementedError(
                "true variable-rate SpeedRamp requires orchestrator-level "
                "frame-rate retiming; not yet implemented. Use a constant "
                "factor or one that folds to an ffmpeg expression."
            )
        chain = FilterChain()
        chain.add(FilterNode(name="setpts", positional=[f"PTS/({factor_expr})"]))
        return chain
