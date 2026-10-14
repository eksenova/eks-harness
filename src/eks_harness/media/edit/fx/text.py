from __future__ import annotations

import importlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog.effects import Captions, LowerThird, TextOverlay
from eks_harness.ir.time import TICKS_PER_SECOND
from eks_harness.media.edit.fx.base import (
    EffectContext,
    EffectFrame,
    Floats,
    Image,
    Pixels,
    Static,
    to_uint8,
)
from eks_harness.media.fonts import font_path

skia: Any = importlib.import_module("skia")

REGULAR = 400
HIGHLIGHT = "#ffdc00ff"
OUTLINE = "#000000ff"
TIKTOK_STROKE = 4
WORD_HOLD = 1.0
POP_SECONDS = 0.15
POP_PEAK = 1.2
SUBTITLE_GAP = 1.2


def typeface(font: object, weight: int, media: Callable[[JsonValue], Path]) -> Any:
    raw: JsonValue = font.model_dump(mode="json") if isinstance(font, BaseModel) else font if isinstance(
        font, str) else None
    if raw is None:
        raise NodeFailure("font_unavailable", f"{font!r} is neither a shipped family nor a font file")
    path = font_path(raw, weight, media)
    face = skia.Typeface.MakeFromFile(str(path))
    if face is None:
        raise NodeFailure("font_unavailable", f"{path} is not a font file skia can load",
                          detail={"path": str(path)})
    return face


def make_font(face: Any, size: float) -> Any:
    font = skia.Font(face, float(size))
    font.setEdging(skia.Font.Edging.kAntiAlias)
    font.setHinting(skia.FontHinting.kNone)
    font.setSubpixel(True)
    return font


def paint(rgba: Floats, *, stroke: float = 0.0) -> Any:
    out = skia.Paint()
    out.setAntiAlias(True)
    out.setColor4f(skia.Color4f(float(rgba[0]), float(rgba[1]), float(rgba[2]), float(rgba[3])))
    if stroke > 0.0:
        out.setStyle(skia.Paint.kStroke_Style)
        out.setStrokeWidth(float(stroke))
        out.setStrokeJoin(skia.Paint.kRound_Join)
    return out


class Layer:
    def __init__(self, width: int, height: int) -> None:
        self.pixels = np.zeros((height, width, 4), dtype=np.uint8)
        info = skia.ImageInfo.Make(width, height, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType)
        self.surface = skia.Surface.MakeRasterDirect(info, self.pixels)
        self.canvas: Any = self.surface.getCanvas()

    def onto(self, image: Image) -> Image:
        self.surface.flushAndSubmit()
        source: Pixels = self.pixels.astype(np.float32)
        sa = source[..., 3:4] / 255.0
        if not np.any(sa):
            return image
        dst = image.astype(np.float32)
        da = dst[..., 3:4] / 255.0
        oa = sa + da * (1.0 - sa)
        safe = np.where(oa > 0.0, oa, 1.0)
        rgb = np.where(oa > 0.0, (source[..., :3] + dst[..., :3] * da * (1.0 - sa)) / safe, 0.0)
        out = np.empty_like(image)
        out[..., :3] = to_uint8(rgb.astype(np.float32))
        out[..., 3] = to_uint8((oa[..., 0] * 255.0).astype(np.float32))
        return out


def ascent(font: Any) -> float:
    return -float(font.getMetrics().fAscent)


def descent(font: Any) -> float:
    return float(font.getMetrics().fDescent)


class TextOverlayFx(Static):
    def __init__(self, ctx: EffectContext, effect: TextOverlay) -> None:
        super().__init__(ctx)
        self.text = ctx.text(effect.text, "text")
        self.font = make_font(typeface(effect.font, REGULAR, ctx.media), effect.size)
        self.color = ctx.color(effect.color, "color")
        self.x = ctx.ints(effect.x, "x")
        self.y = ctx.ints(effect.y, "y")
        start = ctx.local(effect.start, "start") if effect.start is not None else 0.0
        if effect.duration is not None:
            end = start + ctx.length(effect.duration, "duration")
        else:
            clip = ctx.clip_seconds
            end = clip if clip is not None else math.inf
        self.visible = (ctx.seconds >= start) & (ctx.seconds < end)

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        if not self.visible[at] or not self.text:
            return image
        layer = Layer(self.ctx.width, self.ctx.height)
        baseline = float(self.y[at]) + ascent(self.font)
        layer.canvas.drawString(self.text, float(self.x[at]), baseline, self.font, paint(self.color))
        return layer.onto(image)


class LowerThirdFx(Static):
    def __init__(self, ctx: EffectContext, effect: LowerThird) -> None:
        super().__init__(ctx)
        title = ctx.text(effect.title, "title")
        subtitle = ctx.text(effect.subtitle, "subtitle")
        face = typeface(effect.font, REGULAR, ctx.media)
        title_font = make_font(face, effect.title_size)
        subtitle_font = make_font(face, effect.subtitle_size)
        color = ctx.color(effect.color, "color")
        width, height = ctx.size
        layer = Layer(width, height)
        top = (height * 2) // 3
        layer.canvas.drawRect(skia.Rect.MakeLTRB(0, top, width, height), paint(ctx.color(effect.bg_color, "bg_color")))
        title_top = top + effect.margin // 2
        self.draw(layer, title, title_font, effect.position, effect.margin, title_top, color)
        if subtitle:
            subtitle_top = title_top + int(effect.title_size * SUBTITLE_GAP)
            self.draw(layer, subtitle, subtitle_font, effect.position, effect.margin, subtitle_top, color)
        layer.surface.flushAndSubmit()
        self.layer = layer.pixels.copy()

    def draw(self, layer: Layer, text: str, font: Any, position: str, margin: int, top: float,
             color: Floats) -> None:
        width = float(font.measureText(text))
        if position == "left":
            x = float(margin)
        elif position == "right":
            x = self.ctx.width - margin - width
        else:
            x = (self.ctx.width - width) / 2.0
        layer.canvas.drawString(text, x, top + ascent(font), font, paint(color))

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        layer = Layer(self.ctx.width, self.ctx.height)
        layer.pixels[...] = self.layer
        return layer.onto(image)


@dataclass(frozen=True)
class Spoken:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Card:
    words: tuple[Spoken, ...]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


class CaptionsFx(Static):
    def __init__(self, ctx: EffectContext, effect: Captions) -> None:
        super().__init__(ctx)
        if ctx.words is None or effect.source not in ctx.words:
            known = ", ".join(sorted(ctx.words)) if ctx.words else "none"
            raise ctx.fail(f"no words for the speech-to-text source {effect.source!r}; sources with words: {known}",
                           "time_unresolved")
        origin = ctx.base.origin
        found = sorted(ctx.words[effect.source], key=lambda word: word.ticks)
        starts = [(word.ticks - origin) / TICKS_PER_SECOND for word in found]
        spoken = [
            Spoken(word.text, start, min(starts[n + 1], start + WORD_HOLD) if n + 1 < len(starts) else start + WORD_HOLD)
            for n, (word, start) in enumerate(zip(found, starts, strict=True))
        ]
        size = effect.max_words_per_card
        self.cards = [Card(tuple(spoken[n:n + size])) for n in range(0, len(spoken), size)]
        self.effect = effect
        self.font = make_font(typeface(effect.font, REGULAR, ctx.media), effect.size)
        self.color = ctx.color(effect.color, "color")
        self.highlight = ctx.color(HIGHLIGHT, "highlight")
        stroke_width = max(effect.stroke_width, TIKTOK_STROKE) if effect.style == "tiktok" else effect.stroke_width
        self.stroke_width = float(stroke_width)
        self.stroke = ctx.color(effect.stroke if effect.stroke is not None else OUTLINE, "stroke")

    def card(self, t: float) -> Card | None:
        for card in self.cards:
            if card.start <= t < card.end:
                return card
        return None

    def top(self) -> float:
        line = ascent(self.font) + descent(self.font)
        if self.effect.position == "top":
            return float(self.effect.margin)
        if self.effect.position == "center":
            return (self.ctx.height - line) / 2.0
        return self.ctx.height - line - self.effect.margin

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        t = float(self.ctx.seconds[at])
        card = self.card(t)
        if card is None:
            return image
        effect = self.effect
        widths = [float(self.font.measureText(word.text)) for word in card.words]
        spacing = max(4.0, effect.size / 6.0)
        cursor = (self.ctx.width - (sum(widths) + spacing * (len(widths) - 1))) / 2.0
        baseline = self.top() + ascent(self.font)
        middle = baseline - ascent(self.font) / 2.0
        layer = Layer(self.ctx.width, self.ctx.height)
        canvas = layer.canvas
        for word, width in zip(card.words, widths, strict=True):
            x = cursor
            cursor += width + spacing
            since = t - word.start
            if effect.style == "kinetic" and since < 0.0:
                continue
            opacity = 1.0
            scale = 1.0
            if effect.word_animation == "fade":
                opacity = min(max(since / POP_SECONDS, 0.0), 1.0)
            elif effect.word_animation == "pop" and 0.0 <= since < POP_SECONDS:
                scale = 1.0 + (POP_PEAK - 1.0) * (1.0 - since / POP_SECONDS)
            if opacity <= 0.0:
                continue
            active = word.start <= t < word.end
            fill = (self.highlight if effect.style == "tiktok" and active else self.color).copy()
            fill[3] *= opacity
            canvas.save()
            canvas.translate(x + width / 2.0, middle)
            canvas.scale(scale, scale)
            canvas.translate(-width / 2.0, baseline - middle)
            if self.stroke_width > 0.0:
                outline = self.stroke.copy()
                outline[3] *= opacity
                canvas.drawString(word.text, 0.0, 0.0, self.font, paint(outline, stroke=self.stroke_width))
            canvas.drawString(word.text, 0.0, 0.0, self.font, paint(fill))
            canvas.restore()
        return layer.onto(image)


__all__ = ["CaptionsFx", "LowerThirdFx", "TextOverlayFx", "typeface"]
