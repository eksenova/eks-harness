from __future__ import annotations

import numpy as np
from PIL import Image as PILImage

from eks_harness.ir.catalog.effects import Crop, Mirror, Pan, RGBSplit, Shake, Zoom
from eks_harness.media.edit.fx.base import (
    EffectContext,
    EffectFrame,
    Image,
    Static,
    shift,
)

RESAMPLE = PILImage.Resampling.BICUBIC


def resample(image: Image, box: tuple[float, float, float, float], size: tuple[int, int]) -> Image:
    source = PILImage.fromarray(np.ascontiguousarray(image), "RGBA").convert("RGBa")
    scaled = source.resize(size, RESAMPLE, box=box).convert("RGBA")
    return np.asarray(scaled, dtype=np.uint8).copy()


class CropFx(Static):
    def __init__(self, ctx: EffectContext, effect: Crop) -> None:
        super().__init__(ctx)
        self.x = ctx.ints(effect.x, "x")
        self.y = ctx.ints(effect.y, "y")
        self.w = ctx.ints(effect.w, "w")
        self.h = ctx.ints(effect.h, "h")
        width, height = ctx.size
        for at in range(len(ctx.seconds)):
            x, y, w, h = int(self.x[at]), int(self.y[at]), int(self.w[at]), int(self.h[at])
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
                raise ctx.fail(f"crop rectangle x={x} y={y} w={w} h={h} at clip frame {at} does not fit in the "
                               f"{width}x{height} clip")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        x, y, w, h = int(self.x[at]), int(self.y[at]), int(self.w[at]), int(self.h[at])
        if (x, y, w, h) == (0, 0, self.ctx.width, self.ctx.height):
            return image
        return resample(image, (x, y, x + w, y + h), self.ctx.size)


class MirrorFx(Static):
    def __init__(self, ctx: EffectContext, effect: Mirror) -> None:
        super().__init__(ctx)
        self.axis = effect.axis

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        out = image
        if self.axis in ("horizontal", "both"):
            out = out[:, ::-1]
        if self.axis in ("vertical", "both"):
            out = out[::-1]
        return np.ascontiguousarray(out)


class ZoomFx(Static):
    def __init__(self, ctx: EffectContext, effect: Zoom) -> None:
        super().__init__(ctx)
        self.scale = ctx.floats(effect.scale, "scale")
        self.cx = ctx.floats(effect.cx, "cx")
        self.cy = ctx.floats(effect.cy, "cy")
        if self.scale.size and float(self.scale.min()) < 1.0:
            raise ctx.fail(f"scale must be at least 1 (zooming out would leave the frame), got "
                           f"{float(self.scale.min()):.4f}")
        for name, values in (("cx", self.cx), ("cy", self.cy)):
            if values.size and (float(values.min()) < 0.0 or float(values.max()) > 1.0):
                raise ctx.fail(f"{name} must stay between 0 and 1")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        scale = float(self.scale[at])
        if scale == 1.0:
            return image
        width, height = self.ctx.size
        w, h = width / scale, height / scale
        x0 = (width - w) * float(self.cx[at])
        y0 = (height - h) * float(self.cy[at])
        return resample(image, (x0, y0, x0 + w, y0 + h), self.ctx.size)


class PanFx(Static):
    def __init__(self, ctx: EffectContext, effect: Pan) -> None:
        super().__init__(ctx)
        self.dx = ctx.ints(effect.dx, "dx")
        self.dy = ctx.ints(effect.dy, "dy")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        return shift(image, -int(self.dx[at]), -int(self.dy[at]), clamp=False)


class ShakeFx(Static):
    def __init__(self, ctx: EffectContext, effect: Shake) -> None:
        super().__init__(ctx)
        intensity = ctx.floats(effect.intensity, "intensity")
        if effect.decay < 0:
            raise ctx.fail(f"decay must not be negative, got {effect.decay}")
        self.intensity = intensity * np.maximum(0.0, 1.0 - effect.decay * np.maximum(ctx.seconds, 0.0))
        self.buckets = np.floor(ctx.seconds * effect.freq_hz).astype(np.int64)
        self.seed = effect.seed

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        reach = float(self.intensity[at])
        if reach <= 0.0:
            return image
        bucket = int(self.buckets[at])
        rng = self.ctx.rng(self.seed, bucket & 0xFFFFFFFF, 1 if bucket < 0 else 0)
        dx, dy = rng.uniform(-reach, reach, size=2).tolist()
        return shift(image, round(dx), round(dy), clamp=True)


class RGBSplitFx(Static):
    def __init__(self, ctx: EffectContext, effect: RGBSplit) -> None:
        super().__init__(ctx)
        self.ox = ctx.ints(effect.offset_x, "offset_x")
        self.oy = ctx.ints(effect.offset_y, "offset_y")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        ox, oy = int(self.ox[at]), int(self.oy[at])
        if ox == 0 and oy == 0:
            return image
        out = image.copy()
        out[..., 0] = shift(image, -ox, -oy, clamp=True)[..., 0]
        out[..., 2] = shift(image, ox, oy, clamp=True)[..., 2]
        return out


__all__ = ["CropFx", "MirrorFx", "PanFx", "RGBSplitFx", "ShakeFx", "ZoomFx", "resample"]
