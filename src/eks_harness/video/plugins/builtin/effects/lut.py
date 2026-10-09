"""3D LUT effect plugin (ffmpeg ``lut3d``).

The IR carries an arbitrary path; existence is verified by ffmpeg at render
time so that LUTs produced by an upstream task can be referenced before they
exist on disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from eks_harness.video.ir.effects import LUT
from eks_harness.video.plugins.base import CompileTarget, Effect
from eks_harness.video.render.ffmpeg_builder import FilterChain, FilterNode

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["LUTPlugin"]


class LUTPlugin(Effect):
    name: ClassVar[str] = "lut"
    model: ClassVar[type] = LUT
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"ffmpeg_graph"})

    def compile_graph(self, ir: LUT, ctx: RenderContext) -> FilterChain:  # type: ignore[override]
        # Windows paths need both forward-slash separators AND the drive-letter
        # colon escaped as ``\:``, wrapped in single quotes - empirically the
        # only form ffmpeg's filtergraph parser accepts. Pass via ``raw_params``
        # so the serializer doesn't double-escape the backslash we just added.
        path = str(ir.path).replace("\\", "/").replace(":", r"\:")
        chain = FilterChain()
        chain.add(FilterNode(name="lut3d", raw_params={"file": f"'{path}'"}))
        return chain
