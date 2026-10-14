from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.time import TICKS_PER_SECOND, Time, TimeError, TimeSet, Word
from eks_harness.media.animate import AnimateError, Timebase, color_rgba, floats

Image = NDArray[np.uint8]
Floats = NDArray[np.float64]
Ints = NDArray[np.int64]
Pixels = NDArray[np.float32]
Reals = NDArray[np.floating[Any]]


@dataclass(frozen=True)
class EffectFrame:
    index: int
    seconds: float
    local: float


class EffectRuntime(Protocol):
    def apply(self, image: Image, frame: EffectFrame) -> Image: ...

    def close(self) -> None: ...


class EffectContext:
    def __init__(self, kind: str, *, base: Timebase, fps: Fraction, frames: range, size: tuple[int, int],
                 media: Callable[[JsonValue], Path], words: Mapping[str, Sequence[Word]] | None) -> None:
        if fps <= 0:
            raise NodeFailure("args_invalid", f"{kind}: fps must be positive, got {fps}")
        width, height = size
        if width <= 0 or height <= 0:
            raise NodeFailure("args_invalid", f"{kind}: clip size must be positive, got {width}x{height}")
        if frames.step != 1:
            raise NodeFailure("args_invalid", f"{kind}: clip frames must be consecutive, got step {frames.step}")
        self.kind = kind
        self.base = base
        self.fps = Fraction(fps)
        self.frames = frames
        self.size = (width, height)
        self.media = media
        self.words = words
        origin = Fraction(base.origin, TICKS_PER_SECOND)
        self.seconds: Floats = np.asarray([float(Fraction(index) / self.fps - origin) for index in frames],
                                          dtype=np.float64)

    @property
    def width(self) -> int:
        return self.size[0]

    @property
    def height(self) -> int:
        return self.size[1]

    @property
    def clip_seconds(self) -> float | None:
        length = self.base.length
        return None if length is None else length / TICKS_PER_SECOND

    def fail(self, message: str, code: str = "args_invalid") -> NodeFailure:
        return NodeFailure(code, f"{self.kind}: {message}", detail={"effect": self.kind})

    def position(self, index: int) -> int:
        if index not in self.frames:
            raise self.fail(f"frame {index} is outside the clip frames {self.frames.start}..{self.frames.stop - 1}",
                            "executor_error")
        return index - self.frames.start

    def check(self, image: Image) -> None:
        expected = (self.height, self.width, 4)
        if image.dtype != np.uint8 or image.shape != expected:
            raise self.fail(f"expects a {self.height}x{self.width}x4 uint8 RGBA image, got {image.shape} "
                            f"{image.dtype}", "executor_error")

    def floats(self, value: Any, field: str) -> Floats:
        try:
            return floats(value, self.base, self.seconds)
        except AnimateError as exc:
            raise self.fail(f"{field}: {exc}") from exc
        except TimeError as exc:
            raise self.fail(f"{field}: {exc}", "time_unresolved") from exc

    def ints(self, value: Any, field: str) -> Ints:
        return np.rint(self.floats(value, field)).astype(np.int64)

    def color(self, value: str, field: str) -> Floats:
        try:
            return color_rgba(value)
        except AnimateError as exc:
            raise self.fail(f"{field}: {exc}") from exc

    def local(self, time: Time, field: str) -> float:
        try:
            return self.base.local_seconds(time)
        except TimeError as exc:
            raise self.fail(f"{field}: {exc}", "time_unresolved") from exc

    def length(self, duration: Time, field: str) -> float:
        seconds = self.local(duration, field)
        if seconds <= 0:
            raise self.fail(f"{field} must be a positive length, got {seconds:.6f}s")
        return seconds

    def times(self, at: Time | TimeSet, field: str) -> list[float]:
        if isinstance(at, Time):
            return [self.local(at, field)]
        try:
            return self.base.local_set(at)
        except TimeError as exc:
            raise self.fail(f"{field}: {exc}", "time_unresolved") from exc

    def path(self, value: object, field: str) -> Path:
        raw: JsonValue = value.model_dump(mode="json") if isinstance(value, BaseModel) else _json(value, self, field)
        path = self.media(raw)
        if not path.is_file():
            raise self.fail(f"{field}: {path} is not a file", "content_unavailable")
        return path

    def text(self, value: object, field: str) -> str:
        if isinstance(value, str):
            return value
        raise self.fail(f"{field} is an unresolved reference {value!r}; resolve refs before rendering",
                        "ref_unresolved")

    def rng(self, seed: int | None, *salt: int) -> np.random.Generator:
        root = self.base.seed if seed is None else seed
        return np.random.default_rng([root & 0xFFFFFFFFFFFFFFFF, *(value & 0xFFFFFFFFFFFFFFFF for value in salt)])


def _json(value: object, ctx: EffectContext, field: str) -> JsonValue:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise ctx.fail(f"{field} is not a media reference: {value!r}")


def split(image: Image) -> tuple[Pixels, Image]:
    return image[..., :3].astype(np.float32), image[..., 3]


def join(rgb: Reals, alpha: Image) -> Image:
    out = np.empty((*rgb.shape[:2], 4), dtype=np.uint8)
    out[..., :3] = np.clip(np.rint(rgb), 0.0, 255.0).astype(np.uint8)
    out[..., 3] = alpha
    return out


def to_uint8(values: Reals) -> Image:
    return np.clip(np.rint(values), 0.0, 255.0).astype(np.uint8)


def premultiply(image: Image) -> Pixels:
    out = image.astype(np.float32)
    out[..., :3] *= out[..., 3:4] / 255.0
    return out


def unpremultiply(values: Pixels) -> Image:
    alpha = np.clip(values[..., 3:4], 0.0, 255.0)
    safe = np.where(alpha > 0.0, alpha, 1.0)
    rgb = np.where(alpha > 0.0, values[..., :3] * 255.0 / safe, 0.0)
    out = np.empty(values.shape, dtype=np.uint8)
    out[..., :3] = to_uint8(rgb.astype(np.float32))
    out[..., 3] = to_uint8(alpha[..., 0])
    return out


def over(dst: Image, src: Pixels, x: int, y: int) -> Image:
    height, width = dst.shape[:2]
    sh, sw = src.shape[:2]
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + sw, width), min(y + sh, height)
    if x1 <= x0 or y1 <= y0:
        return dst
    patch = src[y0 - y:y1 - y, x0 - x:x1 - x]
    region = dst[y0:y1, x0:x1].astype(np.float32)
    sa = patch[..., 3:4] / 255.0
    da = region[..., 3:4] / 255.0
    oa = sa + da * (1.0 - sa)
    safe = np.where(oa > 0.0, oa, 1.0)
    rgb = np.where(oa > 0.0, (patch[..., :3] * sa + region[..., :3] * da * (1.0 - sa)) / safe, 0.0)
    out = dst.copy()
    out[y0:y1, x0:x1, :3] = to_uint8(rgb.astype(np.float32))
    out[y0:y1, x0:x1, 3] = to_uint8((oa[..., 0] * 255.0).astype(np.float32))
    return out


def shift(image: Image, dx: int, dy: int, *, clamp: bool) -> Image:
    if dx == 0 and dy == 0:
        return image
    height, width = image.shape[:2]
    if clamp:
        rows: Ints = np.clip(np.arange(height, dtype=np.int64) - dy, 0, height - 1)
        cols: Ints = np.clip(np.arange(width, dtype=np.int64) - dx, 0, width - 1)
        return image[rows[:, None], cols[None, :]]
    out = np.zeros_like(image)
    xs, xd = (0, dx) if dx >= 0 else (-dx, 0)
    ys, yd = (0, dy) if dy >= 0 else (-dy, 0)
    w, h = width - abs(dx), height - abs(dy)
    if w > 0 and h > 0:
        out[yd:yd + h, xd:xd + w] = image[ys:ys + h, xs:xs + w]
    return out


class Static:
    def __init__(self, ctx: EffectContext) -> None:
        self.ctx = ctx

    def apply(self, image: Image, frame: EffectFrame) -> Image:
        self.ctx.check(image)
        return self.render(image, self.ctx.position(frame.index), frame)

    def render(self, image: Image, at: int, frame: EffectFrame) -> Image:
        raise NotImplementedError

    def close(self) -> None:
        return None


__all__ = [
    "EffectContext",
    "EffectFrame",
    "EffectRuntime",
    "Floats",
    "Image",
    "Ints",
    "Pixels",
    "Reals",
    "Static",
    "join",
    "over",
    "premultiply",
    "shift",
    "split",
    "to_uint8",
    "unpremultiply",
]
