"""Mirror effect plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.ir.effects import Mirror
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


class MirrorPlugin(Effect):
    name: ClassVar[str] = "mirror"
    model: ClassVar[type] = Mirror
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: Mirror, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        chain = FilterChain()
        if ir.axis in ("horizontal", "both"):
            chain.add(FilterNode(name="hflip"))
        if ir.axis in ("vertical", "both"):
            chain.add(FilterNode(name="vflip"))
        return chain
