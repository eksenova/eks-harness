from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image as PILImage
from PIL import UnidentifiedImageError

from eks_harness.ir.catalog.effects import StickerOverlay, Watermark
from eks_harness.media.edit.fx.base import (
    EffectContext,
    EffectFrame,
    Floats,
    Image,
    Pixels,
    Static,
    over,
)

RESIZE = PILImage.Resampling.LANCZOS
ROTATE = PILImage.Resampling.BICUBIC


def load_image(path: Path, ctx: EffectContext, field: str) -> PILImage.Image:
    try:
        with PILImage.open(path) as source:
            return source.convert("RGBA").convert("RGBa")
    except (OSError, UnidentifiedImageError) as exc:
        raise ctx.fail(f"{field}: cannot read the image {path}: {exc}", "content_unavailable") from exc


def positive(values: Floats, ctx: EffectContext, field: str) -> None:
    if values.size and float(values.min()) <= 0.0:
        raise ctx.fail(f"{field} must be positive, got {float(values.min())}")


def scaled(source: PILImage.Image, scale: float) -> PILImage.Image:
    if scale == 1.0:
        return source
    size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    return source.resize(size, RESIZE)


def pixels(source: PILImage.Image) -> Pixels:
    return np.asarray(source.convert("RGBA"), dtype=np.float32)


class WatermarkFx(Static):
    def __init__(self, ctx: EffectContext, effect: Watermark) -> None:
        super().__init__(ctx)
        self.source = load_image(ctx.path(effect.path, "path"), ctx, "path")
        self.x = ctx.ints(effect.x, "x")
        self.y = ctx.ints(effect.y, "y")
        self.opacity = np.clip(ctx.floats(effect.opacity, "opacity"), 0.0, 1.0)
        self.scale = ctx.floats(effect.scale, "scale")
        positive(self.scale, ctx, "scale")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        opacity = float(self.opacity[at])
        if opacity <= 0.0:
            return image
        mark = pixels(scaled(self.source, float(self.scale[at])))
        mark[..., 3] *= np.float32(opacity)
        return over(image, mark, int(self.x[at]), int(self.y[at]))


class StickerFx(Static):
    def __init__(self, ctx: EffectContext, effect: StickerOverlay) -> None:
        super().__init__(ctx)
        self.source = load_image(ctx.path(effect.path, "path"), ctx, "path")
        self.x = ctx.ints(effect.x, "x")
        self.y = ctx.ints(effect.y, "y")
        self.scale = ctx.floats(effect.scale, "scale")
        self.rotation = ctx.floats(effect.rotation, "rotation")
        positive(self.scale, ctx, "scale")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        scale = float(self.scale[at])
        sticker = scaled(self.source, scale)
        width, height = sticker.width, sticker.height
        rotation = float(self.rotation[at])
        if rotation % 360.0 != 0.0:
            sticker = sticker.rotate(rotation, resample=ROTATE, expand=True)
        cx = int(self.x[at]) + width / 2.0
        cy = int(self.y[at]) + height / 2.0
        return over(image, pixels(sticker), round(cx - sticker.width / 2.0), round(cy - sticker.height / 2.0))


__all__ = ["StickerFx", "WatermarkFx", "load_image"]
