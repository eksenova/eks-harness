from __future__ import annotations

import importlib
from typing import Any

import numpy as np

from eks_harness.ir.catalog.effects import VHS, Blur, Datamosh, Glitch, Sharpen
from eks_harness.media.edit.fx.base import (
    EffectContext,
    EffectFrame,
    Floats,
    Image,
    Pixels,
    Static,
    join,
    premultiply,
    split,
    unpremultiply,
)

SHARPEN_SIGMA = 1.0
GLITCH_SHIFT = 0.1
GLITCH_SWAP = 0.25
SCANLINE = np.float32(0.85)
MOSH_GOP = 30
MOSH_BLOCK = 16
MOSH_REACH = 0.08
YCC = np.asarray([[0.299, 0.587, 0.114], [-0.168736, -0.331264, 0.5], [0.5, -0.418688, -0.081312]],
                 dtype=np.float32)
ndimage: Any = importlib.import_module("scipy.ndimage")
RGB = np.linalg.inv(YCC.astype(np.float64)).astype(np.float32)


def gaussian(values: Pixels, sigma: float) -> Pixels:
    return np.asarray(ndimage.gaussian_filter(values, sigma=(sigma, sigma, 0.0), mode="nearest"), dtype=np.float32)


class BlurFx(Static):
    def __init__(self, ctx: EffectContext, effect: Blur) -> None:
        super().__init__(ctx)
        self.radius = ctx.floats(effect.radius, "radius")
        if self.radius.size and float(self.radius.min()) < 0.0:
            raise ctx.fail(f"radius must not be negative, got {float(self.radius.min())}")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        radius = float(self.radius[at])
        if radius <= 0.0:
            return image
        return unpremultiply(gaussian(premultiply(image), radius))


class SharpenFx(Static):
    def __init__(self, ctx: EffectContext, effect: Sharpen) -> None:
        super().__init__(ctx)
        self.amount = ctx.floats(effect.amount, "amount")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        amount = np.float32(self.amount[at])
        if amount == 0.0:
            return image
        rgb, alpha = split(image)
        return join(rgb + (rgb - gaussian(rgb, SHARPEN_SIGMA)) * amount, alpha)


class GlitchFx(Static):
    def __init__(self, ctx: EffectContext, effect: Glitch) -> None:
        super().__init__(ctx)
        self.intensity = ctx.floats(effect.intensity, "intensity")
        self.block = effect.block_size
        self.seed = effect.seed

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        intensity = float(self.intensity[at])
        if intensity <= 0.0:
            return image
        rng = self.ctx.rng(self.seed, frame.index)
        height, width = image.shape[:2]
        reach = max(1, int(width * GLITCH_SHIFT * intensity))
        out = image.copy()
        for top in range(0, height, self.block):
            rows = slice(top, min(height, top + self.block))
            offset = int(rng.integers(-reach, reach + 1))
            if offset:
                out[rows] = np.roll(out[rows], offset, axis=1)
            if rng.random() < GLITCH_SWAP * intensity:
                order = [int(value) for value in rng.permutation(3)] + [3]
                out[rows] = out[rows][..., order]
        return out


class VHSFx(Static):
    def __init__(self, ctx: EffectContext, effect: VHS) -> None:
        super().__init__(ctx)
        if effect.chroma_blur < 0 or effect.noise < 0:
            raise ctx.fail("chroma_blur and noise must not be negative")
        self.scanlines = effect.scanlines
        self.chroma_blur = effect.chroma_blur
        self.noise = effect.noise

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        rgb, alpha = split(image)
        if self.chroma_blur > 0.0:
            ycc = rgb @ YCC.T
            chroma = ndimage.gaussian_filter1d(ycc[..., 1:], sigma=self.chroma_blur, axis=1, mode="nearest")
            ycc[..., 1:] = np.asarray(chroma, dtype=np.float32)
            rgb = ycc @ RGB.T
        if self.scanlines:
            rgb[::2] *= SCANLINE
        if self.noise > 0.0:
            grain = self.ctx.rng(None, frame.index).standard_normal(rgb.shape[:2], dtype=np.float32)
            rgb += (grain * np.float32(255.0 * self.noise))[..., None]
        return join(rgb, alpha)


class DatamoshFx(Static):
    def __init__(self, ctx: EffectContext, effect: Datamosh) -> None:
        super().__init__(ctx)
        self.intensity = ctx.floats(effect.intensity, "intensity")
        width, height = ctx.size
        self.rows = -(-height // MOSH_BLOCK)
        self.cols = -(-width // MOSH_BLOCK)
        self.reach = MOSH_REACH * min(width, height)

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        intensity = float(self.intensity[at])
        if intensity <= 0.0:
            return image
        gop, step = divmod(frame.index, MOSH_GOP)
        vectors: Floats = self.ctx.rng(None, gop).uniform(-1.0, 1.0, size=(self.rows, self.cols, 2))
        drift = vectors * (self.reach * intensity * (step + 1) / MOSH_GOP)
        height, width = image.shape[:2]
        flow = np.repeat(np.repeat(drift, MOSH_BLOCK, axis=0), MOSH_BLOCK, axis=1)[:height, :width]
        rows = np.arange(height, dtype=np.float64)[:, None] - flow[..., 1]
        cols = np.arange(width, dtype=np.float64)[None, :] - flow[..., 0]
        ys = np.clip(np.rint(rows), 0, height - 1).astype(np.int64)
        xs = np.clip(np.rint(cols), 0, width - 1).astype(np.int64)
        return image[ys, xs]


__all__ = ["BlurFx", "DatamoshFx", "GlitchFx", "SharpenFx", "VHSFx", "gaussian"]
