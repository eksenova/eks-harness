"""Text overlay effect plugin (ffmpeg ``drawtext``).

Animated ``x`` / ``y`` are lowered to ffmpeg expression strings via
:func:`animated_to_ffmpeg_expr`. ``start`` and ``duration`` (when both
provided) gate the overlay with an ``enable='between(t,start,start+duration)'``
clause; missing either disables time gating so the text shows for the entire
segment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.effects import TextOverlay
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["TextOverlayPlugin"]


class TextOverlayPlugin(Effect):
    name: ClassVar[str] = "text_overlay"
    model: ClassVar[type] = TextOverlay
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: TextOverlay, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        x_expr = animated_to_ffmpeg_expr(ir.x, ctx.project, ctx.markers)
        y_expr = animated_to_ffmpeg_expr(ir.y, ctx.project, ctx.markers)
        if x_expr is None or y_expr is None:
            raise ValueError("text_overlay: x/y could not be lowered to ffmpeg expressions")

        params: dict[str, object] = {
            "text": ir.text,
            "font": ir.font,
            "fontsize": ir.size,
            "fontcolor": _color_drawtext(ir.color),
            "x": x_expr,
            "y": y_expr,
        }
        if ir.start is not None and ir.duration is not None:
            start_t = float(resolve_time(ir.start, ctx.project, ctx.markers))
            end_t = start_t + float(ir.duration)
            params["enable"] = f"between(t,{start_t:.6f},{end_t:.6f})"

        chain = FilterChain()
        chain.add(FilterNode(name="drawtext", params=params))
        return chain


def _color_drawtext(color: tuple[int, int, int, int]) -> str:
    r, g, b, a = color
    return f"0x{r:02x}{g:02x}{b:02x}@{a / 255:.3f}"
