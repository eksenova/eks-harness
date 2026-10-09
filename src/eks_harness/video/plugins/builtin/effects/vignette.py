"""Vignette effect plugin (ffmpeg ``vignette``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.ir.effects import Vignette
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["VignettePlugin"]


class VignettePlugin(Effect):
    name: ClassVar[str] = "vignette"
    model: ClassVar[type] = Vignette
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Vignette, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        chain = FilterChain()
        chain.add(
            FilterNode(
                name="vignette",
                params={
                    "angle": f"{ir.angle:.6f}",
                    # ffmpeg's vignette filter uses lowercase ``w``/``h`` for
                    # the frame width/height when parsing ``x0``/``y0``
                    # expressions; the previous uppercase form was rejected
                    # with "Undefined constant".
                    "x0": f"(w*{ir.x0:.6f})",
                    "y0": f"(h*{ir.y0:.6f})",
                },
            )
        )
        return chain
