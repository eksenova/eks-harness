"""Lower-third title card rendered with PIL onto the frame.

PIL is lazy-imported so the import surface stays clean for callers that
never use bitmap overlays. Each frame gets a tinted background panel
spanning the lower third, with the title (and optional subtitle) drawn at
``position`` (left / center / right) inset by ``margin`` pixels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import LowerThird
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext


__all__ = ["LowerThirdPlugin"]


class _LowerThirdProcessor(FrameProcessor):
    def __init__(self, ir: LowerThird) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:  # pragma: no cover - explicit user guidance
            raise ImportError(
                "lower_third requires Pillow; install it with `pip install Pillow`"
            ) from exc

        self._Image = Image
        self._ImageDraw = ImageDraw
        self._title = ir.title
        self._subtitle = ir.subtitle
        self._position = ir.position
        self._margin = max(0, ir.margin)
        self._color_rgba = ir.color
        self._bg_rgba = ir.bg_color
        self._title_font = _load_font(ImageFont, ir.font, ir.title_size)
        self._subtitle_font = _load_font(ImageFont, ir.font, ir.subtitle_size)

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        height, width = frame.shape[:2]
        rgb = frame[..., ::-1]
        pil_frame = self._Image.fromarray(np.ascontiguousarray(rgb), mode="RGB").convert("RGBA")

        panel_top = (height * 2) // 3
        panel = self._Image.new("RGBA", (width, height - panel_top), self._bg_rgba)
        pil_frame.alpha_composite(panel, dest=(0, panel_top))

        draw = self._ImageDraw.Draw(pil_frame)
        text_x = _text_x(self._position, width, self._margin)
        anchor_x = {"left": "l", "center": "m", "right": "r"}[self._position]

        title_y = panel_top + self._margin // 2
        draw.text(
            (text_x, title_y),
            self._title,
            font=self._title_font,
            fill=self._color_rgba,
            anchor=f"{anchor_x}a",
        )

        if self._subtitle:
            subtitle_y = title_y + int(self._title_font.size * 1.2)
            draw.text(
                (text_x, subtitle_y),
                self._subtitle,
                font=self._subtitle_font,
                fill=self._color_rgba,
                anchor=f"{anchor_x}a",
            )

        rgb_out = np.asarray(pil_frame.convert("RGB"))
        return np.ascontiguousarray(rgb_out[..., ::-1])


class LowerThirdPlugin(Effect):
    name: ClassVar[str] = "lower_third"
    model: ClassVar[type] = LowerThird
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def open(self, ir: LowerThird, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        return _LowerThirdProcessor(ir)


def _text_x(position: str, width: int, margin: int) -> int:
    if position == "left":
        return margin
    if position == "right":
        return width - margin
    return width // 2


def _load_font(image_font_module: Any, name: str, size: int) -> Any:
    try:
        return image_font_module.truetype(name, size=size)
    except OSError:
        return image_font_module.load_default()
