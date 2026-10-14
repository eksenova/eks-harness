from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog.effects import (
    LUT,
    Brightness,
    ChromaKey,
    ColorGrade,
    Contrast,
    Fade,
    FilmGrain,
    Flash,
    Posterize,
    Saturation,
    Vignette,
)
from eks_harness.media.animate import AnimateError, curve_values
from eks_harness.media.edit.fx.base import (
    EffectContext,
    EffectFrame,
    Image,
    Pixels,
    Reals,
    Static,
    join,
    split,
    to_uint8,
)

LUMA = np.asarray([0.299, 0.587, 0.114], dtype=np.float32)
KEY_DISTANCE = math.sqrt(3.0) * 255.0
TEMPERATURE_GAIN = 0.5
TINT_GAIN = 0.5


class BrightnessFx(Static):
    def __init__(self, ctx: EffectContext, effect: Brightness) -> None:
        super().__init__(ctx)
        self.amount = ctx.floats(effect.amount, "amount")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        amount = float(self.amount[at])
        if amount == 0.0:
            return image
        rgb, alpha = split(image)
        return join(rgb + np.float32(amount * 255.0), alpha)


class ContrastFx(Static):
    def __init__(self, ctx: EffectContext, effect: Contrast) -> None:
        super().__init__(ctx)
        self.amount = ctx.floats(effect.amount, "amount")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        amount = np.float32(self.amount[at])
        rgb, alpha = split(image)
        return join((rgb - 127.5) * amount + 127.5, alpha)


def saturate(rgb: Pixels, amount: float) -> Reals:
    gray = (rgb @ LUMA)[..., None]
    return gray + (rgb - gray) * np.float32(amount)


class SaturationFx(Static):
    def __init__(self, ctx: EffectContext, effect: Saturation) -> None:
        super().__init__(ctx)
        self.amount = ctx.floats(effect.amount, "amount")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        rgb, alpha = split(image)
        return join(saturate(rgb, float(self.amount[at])), alpha)


@dataclass(frozen=True)
class Cube:
    size: int
    table: Pixels
    low: Pixels
    high: Pixels

    def apply(self, rgb: Reals) -> Reals:
        span = np.where(self.high > self.low, self.high - self.low, np.float32(1.0))
        unit = np.clip((rgb / np.float32(255.0) - self.low) / span, 0.0, 1.0) * np.float32(self.size - 1)
        lower = np.minimum(np.floor(unit).astype(np.int64), self.size - 2)
        frac = (unit - lower).astype(np.float32)
        r0, g0, b0 = lower[..., 0], lower[..., 1], lower[..., 2]
        fr, fg, fb = frac[..., 0:1], frac[..., 1:2], frac[..., 2:3]
        table = self.table

        def corner(db: int, dg: int, dr: int) -> Pixels:
            return table[b0 + db, g0 + dg, r0 + dr]

        c00 = corner(0, 0, 0) * (1 - fr) + corner(0, 0, 1) * fr
        c01 = corner(0, 1, 0) * (1 - fr) + corner(0, 1, 1) * fr
        c10 = corner(1, 0, 0) * (1 - fr) + corner(1, 0, 1) * fr
        c11 = corner(1, 1, 0) * (1 - fr) + corner(1, 1, 1) * fr
        c0 = c00 * (1 - fg) + c01 * fg
        c1 = c10 * (1 - fg) + c11 * fg
        return (c0 * (1 - fb) + c1 * fb) * np.float32(255.0)


def load_cube(path: Path, ctx: EffectContext, field: str) -> Cube:
    def bad(message: str) -> NodeFailure:
        return ctx.fail(f"{field}: {path.name} is not a usable .cube 3D LUT: {message}")

    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ctx.fail(f"{field}: cannot read {path}: {exc}", "content_unavailable") from exc
    size: int | None = None
    low = [0.0, 0.0, 0.0]
    high = [1.0, 1.0, 1.0]
    rows: list[list[float]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        keyword = head.upper()
        try:
            if keyword == "TITLE":
                continue
            if keyword == "LUT_1D_SIZE":
                raise bad("1D LUTs are not supported; give a 3D LUT (LUT_3D_SIZE)")
            if keyword == "LUT_3D_SIZE":
                size = int(rest.strip())
            elif keyword == "DOMAIN_MIN":
                low = [float(part) for part in rest.split()]
            elif keyword == "DOMAIN_MAX":
                high = [float(part) for part in rest.split()]
            elif keyword == "LUT_3D_INPUT_RANGE":
                lo, hi = (float(part) for part in rest.split())
                low, high = [lo] * 3, [hi] * 3
            elif keyword[:1].isalpha():
                raise bad(f"unknown keyword {head!r} on line {number}")
            else:
                rows.append([float(part) for part in line.split()])
        except ValueError as exc:
            raise bad(f"line {number}: {exc}") from exc
    if size is None or size < 2:
        raise bad("LUT_3D_SIZE is missing or below 2")
    if len(low) != 3 or len(high) != 3:
        raise bad("DOMAIN_MIN and DOMAIN_MAX need three values")
    if len(rows) != size**3 or any(len(row) != 3 for row in rows):
        raise bad(f"expected {size**3} rows of three values, got {len(rows)}")
    table = np.asarray(rows, dtype=np.float32).reshape(size, size, size, 3)
    return Cube(size=size, table=table, low=np.asarray(low, dtype=np.float32),
                high=np.asarray(high, dtype=np.float32))


class LutFx(Static):
    def __init__(self, ctx: EffectContext, effect: LUT) -> None:
        super().__init__(ctx)
        self.cube = load_cube(ctx.path(effect.path, "path"), ctx, "path")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        rgb, alpha = split(image)
        return join(self.cube.apply(rgb), alpha)


class ColorGradeFx(Static):
    def __init__(self, ctx: EffectContext, effect: ColorGrade) -> None:
        super().__init__(ctx)
        self.cube = load_cube(ctx.path(effect.lut, "lut"), ctx, "lut") if effect.lut is not None else None
        self.exposure = ctx.floats(effect.exposure, "exposure")
        self.temperature = ctx.floats(effect.temperature, "temperature")
        self.tint = ctx.floats(effect.tint, "tint")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        source, alpha = split(image)
        rgb = self.cube.apply(source) if self.cube is not None else source
        temperature = float(self.temperature[at]) * TEMPERATURE_GAIN
        tint = float(self.tint[at]) * TINT_GAIN
        gain = 2.0 ** float(self.exposure[at])
        scale = np.asarray([gain * (1.0 + temperature), gain * (1.0 + tint), gain * (1.0 - temperature)],
                           dtype=np.float32)
        return join(rgb * scale, alpha)


class InvertFx(Static):
    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        out = image.copy()
        out[..., :3] = 255 - image[..., :3]
        return out


class PosterizeFx(Static):
    def __init__(self, ctx: EffectContext, effect: Posterize) -> None:
        super().__init__(ctx)
        self.levels = ctx.ints(effect.levels, "levels")
        if self.levels.size and int(self.levels.min()) < 2:
            raise ctx.fail(f"levels must be at least 2, got {int(self.levels.min())}")

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        levels = int(self.levels[at])
        if levels >= 256:
            return image
        rgb, alpha = split(image)
        steps = np.minimum(np.floor(rgb * np.float32(levels / 256.0)), np.float32(levels - 1))
        return join(steps * np.float32(255.0 / (levels - 1)), alpha)


class VignetteFx(Static):
    def __init__(self, ctx: EffectContext, effect: Vignette) -> None:
        super().__init__(ctx)
        if not 0.0 <= effect.angle <= math.pi / 2:
            raise ctx.fail(f"angle must be between 0 and pi/2, got {effect.angle}")
        width, height = ctx.size
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
        distance = np.hypot(xs - effect.x0 * width, ys - effect.y0 * height) / math.hypot(width / 2, height / 2)
        cosine = np.cos(effect.angle * distance)
        self.factor = np.where(distance > 1.0, 0.0, cosine**4).astype(np.float32)[..., None]

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        rgb, alpha = split(image)
        return join(rgb * self.factor, alpha)


class ChromaKeyFx(Static):
    def __init__(self, ctx: EffectContext, effect: ChromaKey) -> None:
        super().__init__(ctx)
        self.key = (ctx.color(effect.color, "color")[:3] * 255.0).astype(np.float32)
        self.similarity = np.float32(effect.similarity)
        self.blend = np.float32(effect.blend)

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        rgb, alpha = split(image)
        distance = np.sqrt(np.sum((rgb - self.key) ** 2, axis=-1)) / np.float32(KEY_DISTANCE)
        if self.blend > 0.0:
            keep = np.clip((distance - self.similarity) / self.blend, 0.0, 1.0)
        else:
            keep = (distance > self.similarity).astype(np.float32)
        out = image.copy()
        out[..., 3] = to_uint8(alpha.astype(np.float32) * keep)
        return out


class FadeFx(Static):
    def __init__(self, ctx: EffectContext, effect: Fade) -> None:
        super().__init__(ctx)
        duration = ctx.length(effect.duration, "duration")
        if effect.direction == "in":
            level = ctx.seconds / duration
        else:
            clip = ctx.clip_seconds
            if clip is None:
                raise ctx.fail("a fade out needs the clip length; the clip has no end")
            level = (clip - ctx.seconds) / duration
        self.level = np.clip(level, 0.0, 1.0)

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        level = float(self.level[at])
        if level >= 1.0:
            return image
        out = image.copy()
        out[..., 3] = to_uint8(image[..., 3].astype(np.float32) * np.float32(level))
        return out


class FlashFx(Static):
    def __init__(self, ctx: EffectContext, effect: Flash) -> None:
        super().__init__(ctx)
        duration = ctx.length(effect.duration, "duration")
        rgba = ctx.color(effect.color, "color")
        self.rgb = (rgba[:3] * 255.0).astype(np.float32)
        self.level = np.zeros_like(ctx.seconds)
        for start in ctx.times(effect.at, "at"):
            delta = ctx.seconds - start
            inside = (delta >= 0.0) & (delta < duration)
            if not np.any(inside):
                continue
            if effect.curve is None:
                shape = 1.0 - delta[inside] / duration
            else:
                try:
                    shape = curve_values(effect.curve, ctx.base, delta[inside])
                except AnimateError as exc:
                    raise ctx.fail(f"curve: {exc}") from exc
            self.level[inside] = np.maximum(self.level[inside], np.clip(shape, 0.0, 1.0))
        self.level *= float(rgba[3])

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        level = np.float32(self.level[at])
        if level <= 0.0:
            return image
        rgb, alpha = split(image)
        return join(rgb * (1.0 - level) + self.rgb * level, alpha)


class FilmGrainFx(Static):
    def __init__(self, ctx: EffectContext, effect: FilmGrain) -> None:
        super().__init__(ctx)
        self.intensity = ctx.floats(effect.intensity, "intensity")
        self.seed = effect.seed

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        intensity = float(self.intensity[at])
        if intensity <= 0.0:
            return image
        noise = self.ctx.rng(self.seed, frame.index).standard_normal(image.shape[:2] + (3,), dtype=np.float32)
        rgb, alpha = split(image)
        return join(rgb + noise * np.float32(intensity), alpha)


__all__ = [
    "BrightnessFx",
    "ChromaKeyFx",
    "ColorGradeFx",
    "ContrastFx",
    "Cube",
    "FadeFx",
    "FilmGrainFx",
    "FlashFx",
    "InvertFx",
    "LutFx",
    "PosterizeFx",
    "SaturationFx",
    "VignetteFx",
    "load_cube",
    "saturate",
]
