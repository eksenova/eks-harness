from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from scipy import ndimage

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog.transitions import TransitionBase

Layer = NDArray[np.float64]
BLEND_HOOK = "blend"
FLASH_DECAY = 0.12


@dataclass(frozen=True)
class TransitionFrame:
    progress: float
    index: int
    seconds: float
    seed: int
    beats: Sequence[float] = ()


def _lerp(a: Layer, b: Layer, p: float) -> Layer:
    return a * (1.0 - p) + b * p


def _solid(shape: tuple[int, ...], rgb: tuple[float, float, float]) -> Layer:
    out = np.empty(shape, dtype=np.float64)
    out[..., 0], out[..., 1], out[..., 2], out[..., 3] = rgb[0], rgb[1], rgb[2], 1.0
    return out


def _axis(shape: tuple[int, ...], direction: str) -> Layer:
    height, width = shape[0], shape[1]
    if direction in ("left", "right"):
        ramp = (np.arange(width, dtype=np.float64) + 0.5) / width
        ramp = ramp if direction == "right" else 1.0 - ramp
        return np.broadcast_to(ramp[None, :], (height, width))
    ramp = (np.arange(height, dtype=np.float64) + 0.5) / height
    ramp = ramp if direction == "down" else 1.0 - ramp
    return np.broadcast_to(ramp[:, None], (height, width))


def _mask(a: Layer, b: Layer, mask: Layer) -> Layer:
    return a * (1.0 - mask[..., None]) + b * mask[..., None]


def _shift(layer: Layer, dx: int, dy: int) -> Layer:
    out = np.zeros_like(layer)
    height, width = layer.shape[:2]
    xs, xd = (0, dx) if dx >= 0 else (-dx, 0)
    ys, yd = (0, dy) if dy >= 0 else (-dy, 0)
    w, h = width - abs(dx), height - abs(dy)
    if w > 0 and h > 0:
        out[yd:yd + h, xd:xd + w] = layer[ys:ys + h, xs:xs + w]
    return out


def _offset(direction: str, amount: float, shape: tuple[int, ...]) -> tuple[int, int]:
    height, width = shape[0], shape[1]
    if direction == "right":
        return round(amount * width), 0
    if direction == "left":
        return -round(amount * width), 0
    if direction == "down":
        return 0, round(amount * height)
    return 0, -round(amount * height)


def _blur(layer: Layer, sigma: float) -> Layer:
    if sigma <= 0.05:
        return layer
    return ndimage.gaussian_filter(layer, sigma=(sigma, sigma, 0), mode="nearest")


def _zoom(layer: Layer, factor: float) -> Layer:
    if abs(factor - 1.0) < 1e-6:
        return layer
    height, width = layer.shape[:2]
    out = np.empty_like(layer)
    for channel in range(layer.shape[2]):
        out[..., channel] = ndimage.affine_transform(
            layer[..., channel], np.diag([1.0 / factor, 1.0 / factor]),
            offset=cast(Any, ((height - height / factor) / 2.0, (width - width / factor) / 2.0)), order=1,
            mode="nearest")
    return out


def _pixelize(layer: Layer, block: int) -> Layer:
    if block <= 1:
        return layer
    height, width = layer.shape[:2]
    small = layer[::block, ::block]
    grown = np.repeat(np.repeat(small, block, axis=0), block, axis=1)
    return grown[:height, :width]


def _gray(layer: Layer, amount: float) -> Layer:
    luma = layer[..., 0] * 0.2126 + layer[..., 1] * 0.7152 + layer[..., 2] * 0.0722
    out = layer.copy()
    for channel in range(3):
        out[..., channel] = layer[..., channel] * (1.0 - amount) + luma * amount
    return out


def _rgb_split(layer: Layer, pixels: int) -> Layer:
    if pixels == 0:
        return layer
    out = layer.copy()
    out[..., 0] = _shift(layer, pixels, 0)[..., 0]
    out[..., 2] = _shift(layer, -pixels, 0)[..., 2]
    return out


def _peak(p: float) -> float:
    return math.sin(math.pi * min(max(p, 0.0), 1.0))


def _bloom(shape: tuple[int, ...], seed: int, warm: tuple[float, float, float]) -> Layer:
    rng = np.random.default_rng(seed)
    height, width = shape[0], shape[1]
    cx, cy = rng.uniform(0.2, 0.8) * width, rng.uniform(0.2, 0.8) * height
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    radius = 0.6 * math.hypot(width, height)
    glow = np.clip(1.0 - np.hypot(xs - cx, ys - cy) / radius, 0.0, 1.0) ** 2
    out = np.zeros((height, width, 4), dtype=np.float64)
    out[..., 0], out[..., 1], out[..., 2] = glow * warm[0], glow * warm[1], glow * warm[2]
    return out


def _add(layer: Layer, light: Layer, amount: float) -> Layer:
    out = layer.copy()
    out[..., :3] = np.clip(layer[..., :3] + light[..., :3] * amount * np.maximum(layer[..., 3:4], 0.0), 0.0, 1.0)
    return out


def _glitch(layer: Layer, seed: int, intensity: float) -> Layer:
    rng = np.random.default_rng(seed)
    out = layer.copy()
    height, width = layer.shape[:2]
    for _slice in range(max(1, round(8 * intensity))):
        top = int(rng.integers(0, max(height - 1, 1)))
        size = int(rng.integers(1, max(2, height // 12)))
        dx = int(rng.integers(-max(1, width // 10), max(2, width // 10)))
        out[top:top + size] = _shift(layer[top:top + size], dx, 0)
    return _rgb_split(out, round(intensity * width / 80))


def _beat_level(frame: TransitionFrame) -> float:
    level = 0.0
    for beat in frame.beats:
        delta = frame.seconds - beat
        if 0.0 <= delta < 6 * FLASH_DECAY:
            level = max(level, math.exp(-delta / FLASH_DECAY))
    return level


def blend(transition: TransitionBase, a: Layer, b: Layer, frame: TransitionFrame) -> Layer:
    p = min(max(frame.progress, 0.0), 1.0)
    kind = transition.kind
    shape = a.shape
    values = transition.model_dump()
    direction = str(values.get("direction") or "right")
    intensity = float(values.get("intensity") or 0.0)
    if kind == "cut":
        return b if p > 0.0 else a
    if kind == "crossfade":
        return _lerp(a, b, p)
    if kind in ("dip_to_black", "dip_to_white"):
        tone = _solid(shape, (0.0, 0.0, 0.0) if kind == "dip_to_black" else (1.0, 1.0, 1.0))
        return _lerp(a, tone, p * 2.0) if p < 0.5 else _lerp(tone, b, p * 2.0 - 1.0)
    if kind == "wipe":
        return _mask(a, b, (_axis(shape, direction) <= p).astype(np.float64))
    if kind == "slide":
        dx, dy = _offset(direction, p - 1.0, shape)
        moved = _shift(b, dx, dy)
        return moved + a * (1.0 - moved[..., 3:4])
    if kind == "push":
        ax, ay = _offset(direction, p, shape)
        bx, by = _offset(direction, p - 1.0, shape)
        return _shift(a, ax, ay) + _shift(b, bx, by)
    if kind == "zoom":
        return _lerp(_zoom(a, 1.0 + p), _zoom(b, 1.0 + (1.0 - p) * 0.5), p)
    if kind == "dissolve":
        noise = np.random.default_rng(frame.seed).random(shape[:2])
        return _mask(a, b, (noise < p).astype(np.float64))
    if kind == "blur_through":
        sigma = 12.0 * _peak(p)
        return _lerp(_blur(a, sigma), _blur(b, sigma), p)
    if kind == "iris":
        height, width = shape[0], shape[1]
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
        distance = np.hypot(xs - width / 2.0, ys - height / 2.0) / (math.hypot(width, height) / 2.0)
        if values.get("mode") == "close":
            return _mask(a, b, (distance > 1.0 - p).astype(np.float64))
        return _mask(a, b, (distance <= p).astype(np.float64))
    if kind == "radial":
        height, width = shape[0], shape[1]
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
        angle = (np.arctan2(xs - width / 2.0, -(ys - height / 2.0)) + 2.0 * math.pi) % (2.0 * math.pi)
        return _mask(a, b, (angle <= p * 2.0 * math.pi).astype(np.float64))
    if kind == "pixelize":
        block = 1 + round(_peak(p) * max(shape[0], shape[1]) / 24)
        return _lerp(_pixelize(a, block), _pixelize(b, block), p)
    if kind == "fade_grays":
        return _gray(a, p * 2.0) if p < 0.5 else _gray(b, 2.0 - p * 2.0)
    if kind == "luma_wipe":
        feather = 0.2
        edge = np.clip((p * (1.0 + feather) - _axis(shape, direction)) / feather, 0.0, 1.0)
        return _mask(a, b, edge)
    if kind == "rgb_shift_wipe":
        mixed = _mask(a, b, (_axis(shape, direction) <= p).astype(np.float64))
        return _rgb_split(mixed, round(_peak(p) * intensity * shape[1] / 40))
    if kind == "whip_pan":
        ax, ay = _offset(direction, p, shape)
        bx, by = _offset(direction, p - 1.0, shape)
        moved = _shift(a, ax, ay) + _shift(b, bx, by)
        sigma = _peak(p) * intensity * shape[1] / 30
        return ndimage.gaussian_filter1d(moved, sigma, axis=1 if direction in ("left", "right") else 0,
                                         mode="nearest") if sigma > 0.05 else moved
    if kind == "light_leak":
        return _add(_lerp(a, b, p), _bloom(shape, frame.seed, (1.0, 0.62, 0.25)), intensity * _peak(p))
    if kind == "film_burn":
        burn = _bloom(shape, frame.seed, (1.0, 0.45, 0.1))
        noise = np.random.default_rng(frame.seed + frame.index).random(shape[:2])
        burn[..., :3] *= (0.6 + 0.4 * noise)[..., None]
        return _add(_lerp(a, b, p), burn, intensity * _peak(p) * 1.5)
    if kind == "glitch_transition":
        block = 1 + round(_peak(p) * intensity * shape[1] / 30)
        mixed = _lerp(_pixelize(a, block), _pixelize(b, block), p)
        return _glitch(mixed, frame.seed + frame.index, intensity * _peak(p))
    if kind == "beat_flash":
        white = _solid(shape, (1.0, 1.0, 1.0))
        mixed = _lerp(a, b, p)
        return _lerp(mixed, white * mixed[..., 3:4], intensity * _beat_level(frame))
    if kind == "beat_glitch":
        level = _beat_level(frame)
        block = 1 + round(level * intensity * shape[1] / 30)
        mixed = _lerp(_pixelize(a, block), _pixelize(b, block), p)
        return _glitch(mixed, frame.seed + frame.index, intensity * level) if level > 0.01 else mixed
    hook = getattr(transition, BLEND_HOOK, None)
    if callable(hook):
        call = cast(Callable[[Layer, Layer, TransitionFrame], Layer], hook)
        found = np.asarray(call(a, b, frame), dtype=np.float64)
        if found.shape != a.shape:
            raise NodeFailure("executor_error", f"transition {kind} returned a layer of shape {found.shape}, "
                              f"expected {a.shape}")
        return found
    raise NodeFailure("executor_error", f"transition {kind!r} has no {BLEND_HOOK}(a, b, frame) method, so the "
                      "renderer cannot apply it")


__all__ = ["TransitionFrame", "blend"]
