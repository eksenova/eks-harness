"""Captions effect: per-frame STT-driven text overlay.

The plugin runs frame-pipeline only -- ffmpeg's ``drawtext`` filter cannot
express per-word emphasis or kinetic per-word animation cleanly, so we
composite text onto frames in-process. Rendering uses skia-python when
available (sub-pixel anti-aliasing, proper stroke), falling back to PIL on
hosts that do not have skia installed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from eks_harness.video.ir.effects import Captions
from eks_harness.video.plugins.base import CompileTarget, Effect, FrameProcessor

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import WordHit
    from eks_harness.video.plugins.builtin.captioners import WordToken
    from eks_harness.video.render.context import RenderContext

__all__ = ["CaptionsPlugin", "CaptionsProcessor"]

_LOG = logging.getLogger(__name__)
_PIL_FALLBACK_WARNED = False
_FALLBACK_FONT_LOCK = threading.Lock()
_TIKTOK_HIGHLIGHT_RGBA: tuple[int, int, int, int] = (255, 220, 0, 255)
_POP_DURATION_SECONDS = 0.15
_POP_PEAK_SCALE = 1.2


class CaptionsPlugin(Effect):
    """Plugin that registers the :class:`Captions` IR with the renderer."""

    name: ClassVar[str] = "captions"
    model: ClassVar[type] = Captions
    compile_targets: ClassVar[frozenset[CompileTarget]] = frozenset({"frame_pipeline"})

    def prefers_frame_pipeline(self, ir: Captions, ctx: RenderContext) -> bool:  # type: ignore[override]
        return True

    def open(self, ir: Captions, ctx: RenderContext) -> FrameProcessor:  # type: ignore[override]
        words = _resolve_words(ir.source, ctx)
        return CaptionsProcessor(ir=ir, words=words, fps=ctx.project.fps)


class CaptionsProcessor(FrameProcessor):
    """Frame processor that composites caption cards onto each frame."""

    def __init__(
        self,
        ir: Captions,
        words: Sequence[WordToken],
        fps: float = 30.0,
    ) -> None:
        self._ir = ir
        self._words: list[WordToken] = list(words)
        self._fps = float(fps)
        self._cards = _group_into_cards(self._words, ir.max_words_per_card)
        self._renderer = _select_renderer(ir)

    def process(self, frame: Any, t: float, frame_idx: int) -> Any:
        card = _active_card(self._cards, t)
        if card is None:
            return frame
        return self._renderer.render(frame, card, t)


# --- card grouping -----------------------------------------------------------


class _CaptionWord:
    __slots__ = ("end", "start", "text")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _CaptionCard:
    __slots__ = ("end", "start", "words")

    def __init__(self, words: list[_CaptionWord]) -> None:
        self.words = words
        self.start = words[0].start
        self.end = words[-1].end


def _group_into_cards(words: Sequence[WordToken], max_per_card: int) -> list[_CaptionCard]:
    if max_per_card <= 0 or not words:
        return []
    cards: list[_CaptionCard] = []
    current: list[_CaptionWord] = []
    for word in words:
        current.append(_CaptionWord(text=word.text, start=float(word.start), end=float(word.end)))
        if len(current) >= max_per_card:
            cards.append(_CaptionCard(current))
            current = []
    if current:
        cards.append(_CaptionCard(current))
    return cards


def _active_card(cards: Sequence[_CaptionCard], t: float) -> _CaptionCard | None:
    for card in cards:
        if card.start <= t <= card.end:
            return card
    return None


def _resolve_words(source_name: str, ctx: RenderContext) -> list[WordToken]:
    from eks_harness.video.plugins.builtin.captioners import WordToken

    out: list[WordToken] = []
    hits: list[WordHit] = [
        hit for hit in ctx.markers.words if hit.source == source_name
    ]
    if not hits and source_name in ctx.markers.named:
        return out
    for hit in hits:
        out.append(WordToken(text=hit.text, start=float(hit.t_start), end=float(hit.t_end)))
    out.sort(key=lambda w: w.start)
    return out


# --- rendering ---------------------------------------------------------------


class _CardRenderer:
    """Strategy that paints one caption card onto a BGR frame."""

    def __init__(self, ir: Captions) -> None:
        self._ir = ir

    def render(self, frame: np.ndarray, card: _CaptionCard, t: float) -> np.ndarray:
        raise NotImplementedError


def _select_renderer(ir: Captions) -> _CardRenderer:
    skia = _try_import_skia()
    if skia is not None:
        return _SkiaRenderer(ir, skia)
    _warn_pil_fallback_once()
    return _PILRenderer(ir)


def _try_import_skia() -> Any | None:
    try:
        import skia  # type: ignore[import-not-found]
    except ImportError:
        return None
    return skia


def _warn_pil_fallback_once() -> None:
    global _PIL_FALLBACK_WARNED
    if _PIL_FALLBACK_WARNED:
        return
    _LOG.warning(
        "skia-python not installed; falling back to PIL for caption rendering "
        "(install `eks-harness[ml]` for higher-quality output)"
    )
    _PIL_FALLBACK_WARNED = True


def _word_pop_scale(card: _CaptionCard, word: _CaptionWord, t: float) -> float:
    half = _POP_DURATION_SECONDS * 0.5
    delta = abs(t - word.start)
    if delta >= half:
        return 1.0
    progress = 1.0 - (delta / half)
    return 1.0 + (_POP_PEAK_SCALE - 1.0) * progress


def _resolve_y(position: str, frame_h: int, text_h: int, margin: int) -> int:
    if position == "top":
        return max(0, margin)
    if position == "center":
        return max(0, (frame_h - text_h) // 2)
    return max(0, frame_h - text_h - margin)


# --- PIL renderer ------------------------------------------------------------


class _PILRenderer(_CardRenderer):
    def __init__(self, ir: Captions) -> None:
        super().__init__(ir)
        self._font = _load_pil_font(ir.font, ir.size)

    def render(self, frame: np.ndarray, card: _CaptionCard, t: float) -> np.ndarray:
        from PIL import Image, ImageDraw

        bgr = frame
        rgb = bgr[:, :, ::-1]
        image = Image.fromarray(rgb).convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        widths, heights = self._measure(draw, card)
        spacing = max(4, self._ir.size // 6)
        line_w = sum(widths) + spacing * (len(widths) - 1)
        line_h = max(heights) if heights else self._ir.size

        x = max(0, (image.width - line_w) // 2)
        y = _resolve_y(self._ir.position, image.height, line_h, self._ir.margin)

        active_word = self._active_word(card, t)
        cursor = x
        for word, w_width in zip(card.words, widths, strict=False):
            color = self._word_color(card, word, active_word)
            self._draw_word(overlay, draw, word, cursor, y, color, t, card)
            cursor += w_width + spacing

        composited = Image.alpha_composite(image, overlay).convert("RGB")
        return np.asarray(composited)[:, :, ::-1].copy()

    def _measure(
        self, draw: Any, card: _CaptionCard
    ) -> tuple[list[int], list[int]]:
        widths: list[int] = []
        heights: list[int] = []
        for word in card.words:
            bbox = draw.textbbox((0, 0), word.text, font=self._font)
            widths.append(bbox[2] - bbox[0])
            heights.append(bbox[3] - bbox[1])
        return widths, heights

    def _active_word(self, card: _CaptionCard, t: float) -> _CaptionWord | None:
        for word in card.words:
            if word.start <= t <= word.end:
                return word
        return None

    def _word_color(
        self,
        card: _CaptionCard,
        word: _CaptionWord,
        active: _CaptionWord | None,
    ) -> tuple[int, int, int, int]:
        if self._ir.style == "tiktok" and active is word:
            return _TIKTOK_HIGHLIGHT_RGBA
        return self._ir.color

    def _draw_word(
        self,
        overlay: Any,
        draw: Any,
        word: _CaptionWord,
        x: int,
        y: int,
        color: tuple[int, int, int, int],
        t: float,
        card: _CaptionCard,
    ) -> None:
        stroke_width = self._ir.stroke_width
        if self._ir.style == "tiktok":
            stroke_width = max(stroke_width, 4)
        stroke_fill = self._ir.stroke if self._ir.stroke is not None else (0, 0, 0, 255)
        if self._ir.style == "kinetic":
            self._draw_kinetic(overlay, draw, word, x, y, color, stroke_width, stroke_fill, t, card)
            return
        draw.text(
            (x, y),
            word.text,
            font=self._font,
            fill=color,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )

    def _draw_kinetic(
        self,
        overlay: Any,
        draw: Any,
        word: _CaptionWord,
        x: int,
        y: int,
        color: tuple[int, int, int, int],
        stroke_width: int,
        stroke_fill: tuple[int, int, int, int],
        t: float,
        card: _CaptionCard,
    ) -> None:
        from PIL import Image, ImageDraw

        scale = _word_pop_scale(card, word, t)
        if abs(scale - 1.0) < 1e-3:
            draw.text(
                (x, y),
                word.text,
                font=self._font,
                fill=color,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
            return

        bbox = draw.textbbox((0, 0), word.text, font=self._font)
        w = max(1, bbox[2] - bbox[0] + stroke_width * 2)
        h = max(1, bbox[3] - bbox[1] + stroke_width * 2)
        sprite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        sprite_draw = ImageDraw.Draw(sprite)
        sprite_draw.text(
            (stroke_width - bbox[0], stroke_width - bbox[1]),
            word.text,
            font=self._font,
            fill=color,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        scaled = sprite.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            resample=Image.Resampling.LANCZOS,
        )
        offset_x = x - (scaled.width - w) // 2
        offset_y = y - (scaled.height - h) // 2
        overlay.alpha_composite(scaled, dest=(offset_x, offset_y))


def _load_pil_font(family: str, size: int) -> Any:
    from PIL import ImageFont

    with _FALLBACK_FONT_LOCK:
        for candidate in _font_candidates(family):
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
        return ImageFont.load_default()


def _font_candidates(family: str) -> list[str]:
    candidates: list[str] = [family, f"{family}.ttf", f"{family}-Bold.ttf"]
    candidates.extend(
        [
            "arial.ttf",
            "Arial.ttf",
            "DejaVuSans-Bold.ttf",
            "DejaVuSans.ttf",
            "Helvetica.ttc",
        ]
    )
    return candidates


# --- skia renderer -----------------------------------------------------------


class _SkiaRenderer(_CardRenderer):
    def __init__(self, ir: Captions, skia: Any) -> None:
        super().__init__(ir)
        self._skia = skia
        self._typeface = self._load_typeface(ir.font)
        self._font = skia.Font(self._typeface, float(ir.size))

    def render(self, frame: np.ndarray, card: _CaptionCard, t: float) -> np.ndarray:
        skia = self._skia
        bgr = frame
        h, w, _ = bgr.shape
        rgba = np.dstack(
            [bgr[:, :, 2], bgr[:, :, 1], bgr[:, :, 0], np.full((h, w), 255, dtype=np.uint8)]
        )
        rgba_contig = np.ascontiguousarray(rgba)
        image_info = skia.ImageInfo.Make(
            w, h, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType
        )
        surface = skia.Surface.MakeRasterDirect(image_info, rgba_contig)
        canvas = surface.getCanvas()

        widths = [self._font.measureText(word.text) for word in card.words]
        spacing = max(4.0, float(self._ir.size) / 6.0)
        line_w = sum(widths) + spacing * (len(widths) - 1)
        text_height = float(self._ir.size)
        x_start = max(0.0, (w - line_w) / 2.0)
        y = float(_resolve_y(self._ir.position, h, int(text_height), self._ir.margin)) + text_height

        active = self._active_word(card, t)
        cursor = x_start
        for word, word_w in zip(card.words, widths, strict=False):
            color = self._word_color(card, word, active)
            self._draw_word(canvas, word, cursor, y, color, t, card)
            cursor += word_w + spacing

        surface.flushAndSubmit()
        out_rgba = rgba_contig
        out_bgr = np.dstack([out_rgba[:, :, 2], out_rgba[:, :, 1], out_rgba[:, :, 0]])
        return np.ascontiguousarray(out_bgr)

    def _load_typeface(self, family: str) -> Any:
        skia = self._skia
        try:
            return skia.Typeface.MakeFromName(family, skia.FontStyle.Bold())
        except Exception:
            return skia.Typeface()

    def _active_word(self, card: _CaptionCard, t: float) -> _CaptionWord | None:
        for word in card.words:
            if word.start <= t <= word.end:
                return word
        return None

    def _word_color(
        self,
        card: _CaptionCard,
        word: _CaptionWord,
        active: _CaptionWord | None,
    ) -> tuple[int, int, int, int]:
        if self._ir.style == "tiktok" and active is word:
            return _TIKTOK_HIGHLIGHT_RGBA
        return self._ir.color

    def _draw_word(
        self,
        canvas: Any,
        word: _CaptionWord,
        x: float,
        y: float,
        color: tuple[int, int, int, int],
        t: float,
        card: _CaptionCard,
    ) -> None:
        skia = self._skia
        scale = (
            _word_pop_scale(card, word, t) if self._ir.style == "kinetic" else 1.0
        )

        canvas.save()
        canvas.translate(x + self._font.measureText(word.text) / 2.0, y - self._ir.size / 2.0)
        canvas.scale(scale, scale)
        canvas.translate(-self._font.measureText(word.text) / 2.0, self._ir.size / 2.0)

        stroke_width = float(self._ir.stroke_width)
        if self._ir.style == "tiktok":
            stroke_width = max(stroke_width, 4.0)
        if stroke_width > 0:
            stroke_color = self._ir.stroke if self._ir.stroke is not None else (0, 0, 0, 255)
            stroke_paint = skia.Paint(
                Color=skia.Color(*stroke_color),
                Style=skia.Paint.kStroke_Style,
                StrokeWidth=stroke_width,
                AntiAlias=True,
            )
            canvas.drawString(word.text, 0.0, 0.0, self._font, stroke_paint)

        fill_paint = skia.Paint(Color=skia.Color(*color), AntiAlias=True)
        canvas.drawString(word.text, 0.0, 0.0, self._font, fill_paint)
        canvas.restore()


