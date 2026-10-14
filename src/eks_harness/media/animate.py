from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from eks_harness.ir.catalog.animated import Driven
from eks_harness.ir.catalog.curves import (
    LFO,
    BeatPulse,
    Bezier,
    Bounce,
    BoxEnv,
    CurveBase,
    EasingEnv,
    ExpDecay,
    ExpDecayEnv,
    Lambda,
    LinearEnv,
    Noise,
    RandomChoice,
    Sine,
    Spring,
    Step,
    compile_lambda,
)
from eks_harness.ir.catalog.easing import EasingBase
from eks_harness.ir.catalog.keys import Keyframe, Keys
from eks_harness.ir.hashing import derived_seed
from eks_harness.ir.time import TICKS_PER_SECOND, Anchor, Time, TimeContext, TimeSet

Floats = NDArray[np.float64]
LOCAL_ANCHORS = frozenset({"start", "end"})
SMOOTH_NOISE_STEP = 0.5
EASING_HOOK = "evaluate"
CURVE_HOOK = "sample"


class AnimateError(ValueError):
    pass


def _start_anchored(time: Time) -> Time:
    if time.anchor is not None:
        return time
    return Time(anchor=Anchor(kind="start"), bars=time.bars, beats=time.beats, frames=time.frames, ticks=time.ticks)


@dataclass(frozen=True)
class Timebase:
    context: TimeContext
    origin: int = 0
    length: int | None = None
    seed: int = 0
    path: str = ""

    def local_ticks(self, time: Time) -> int:
        if time.anchor is None or time.anchor.kind in LOCAL_ANCHORS:
            return self._localized().resolve(_start_anchored(time)) - self.origin
        return self.context.resolve(time) - self.origin

    def local_seconds(self, time: Time) -> float:
        return self.local_ticks(time) / TICKS_PER_SECOND

    def _localized(self) -> TimeContext:
        return replace(self.context, start=self.origin,
                       end=self.origin + self.length if self.length is not None else self.context.end)

    def local_set(self, times: TimeSet, *, seed: int | None = None) -> list[float]:
        window = times.window
        if window is not None:
            window = window.model_copy(update={"start": _start_anchored(window.start),
                                               "end": _start_anchored(window.end)})
        localized = times.model_copy(update={"times": tuple(_start_anchored(time) for time in times.times),
                                             "window": window})
        found = self._localized().resolve_set(localized, seed=seed if seed is not None else self.seed)
        return [(ticks - self.origin) / TICKS_PER_SECOND for ticks in found]

    def child(self, path: str) -> Timebase:
        return replace(self, path=f"{self.path}/{path}" if self.path else path,
                       seed=derived_seed(self.seed, path))


def _bounce(u: Floats) -> Floats:
    n1, d1 = 7.5625, 2.75
    out = np.empty_like(u)
    first = u < 1 / d1
    second = (u >= 1 / d1) & (u < 2 / d1)
    third = (u >= 2 / d1) & (u < 2.5 / d1)
    last = u >= 2.5 / d1
    out[first] = n1 * u[first] ** 2
    v = u[second] - 1.5 / d1
    out[second] = n1 * v * v + 0.75
    v = u[third] - 2.25 / d1
    out[third] = n1 * v * v + 0.9375
    v = u[last] - 2.625 / d1
    out[last] = n1 * v * v + 0.984375
    return out


def _elastic(u: Floats) -> Floats:
    c4 = (2.0 * math.pi) / 3.0
    out = np.power(2.0, -10.0 * u) * np.sin((u * 10.0 - 0.75) * c4) + 1.0
    return np.where((u <= 0.0) | (u >= 1.0), u, out)


def _back(u: Floats) -> Floats:
    c1 = 1.70158
    c3 = c1 + 1.0
    v = u - 1.0
    return 1.0 + c3 * v * v * v + c1 * v * v


def _in_out(power: float) -> Callable[[Floats], Floats]:
    def apply(u: Floats) -> Floats:
        half = 2.0 ** (power - 1.0)
        return np.where(u < 0.5, half * np.power(u, power), 1.0 - np.power(-2.0 * u + 2.0, power) / 2.0)

    return apply


def _expo_in(u: Floats) -> Floats:
    return np.where(u <= 0.0, 0.0, np.power(2.0, 10.0 * u - 10.0))


def _expo_out(u: Floats) -> Floats:
    return np.where(u >= 1.0, 1.0, 1.0 - np.power(2.0, -10.0 * u))


def _expo_in_out(u: Floats) -> Floats:
    low = np.power(2.0, 20.0 * u - 10.0) / 2.0
    high = (2.0 - np.power(2.0, -20.0 * u + 10.0)) / 2.0
    return np.where(u <= 0.0, 0.0, np.where(u >= 1.0, 1.0, np.where(u < 0.5, low, high)))


EASINGS: dict[str, Callable[[Floats], Floats]] = {
    "linear": lambda u: u,
    "in_quad": lambda u: u * u,
    "out_quad": lambda u: 1.0 - (1.0 - u) ** 2,
    "in_out_quad": _in_out(2.0),
    "in_cubic": lambda u: u**3,
    "out_cubic": lambda u: 1.0 - (1.0 - u) ** 3,
    "in_out_cubic": _in_out(3.0),
    "in_quart": lambda u: u**4,
    "out_quart": lambda u: 1.0 - (1.0 - u) ** 4,
    "in_out_quart": _in_out(4.0),
    "in_sine": lambda u: 1.0 - np.cos(u * math.pi / 2.0),
    "out_sine": lambda u: np.sin(u * math.pi / 2.0),
    "in_out_sine": lambda u: -(np.cos(math.pi * u) - 1.0) / 2.0,
    "in_expo": _expo_in,
    "out_expo": _expo_out,
    "in_out_expo": _expo_in_out,
    "out_bounce": _bounce,
    "out_elastic": _elastic,
    "out_back": _back,
}


def _cubic_bezier(p1x: float, p1y: float, p2x: float, p2y: float, u: Floats) -> Floats:
    out = np.empty_like(u)
    for index, x in enumerate(u.tolist()):
        if x <= 0.0:
            out[index] = 0.0
            continue
        if x >= 1.0:
            out[index] = 1.0
            continue
        lo, hi, s = 0.0, 1.0, x
        for _ in range(60):
            bx = 3.0 * (1 - s) ** 2 * s * p1x + 3.0 * (1 - s) * s * s * p2x + s**3
            if abs(bx - x) < 1e-12:
                break
            if bx < x:
                lo = s
            else:
                hi = s
            s = (lo + hi) / 2.0
        out[index] = 3.0 * (1 - s) ** 2 * s * p1y + 3.0 * (1 - s) * s * s * p2y + s**3
    return out


def ease(easing: EasingBase | None, u: Floats) -> Floats:
    clipped = np.clip(u, 0.0, 1.0)
    if easing is None:
        return clipped
    kind = easing.kind
    if kind == "cubic_bezier":
        values = easing.model_dump()
        return _cubic_bezier(float(values["p1x"]), float(values["p1y"]), float(values["p2x"]),
                             float(values["p2y"]), clipped)
    known = EASINGS.get(kind)
    if known is not None:
        return np.asarray(known(clipped), dtype=np.float64)
    hook = getattr(easing, EASING_HOOK, None)
    if not callable(hook):
        raise AnimateError(f"easing {kind!r} has no {EASING_HOOK}(u) method, so renderers cannot apply it")
    evaluate = cast(Callable[[float], float], hook)
    return np.array([float(evaluate(float(value))) for value in clipped.tolist()], dtype=np.float64)


def _numbers(value: Any) -> Floats:
    if isinstance(value, str):
        return color_rgba(value)
    if isinstance(value, Sequence):
        return np.asarray([float(item) for item in value], dtype=np.float64)
    return np.asarray([float(value)], dtype=np.float64)


def color_rgba(text: str) -> Floats:
    raw = text.lstrip("#")
    if len(raw) == 6:
        raw += "ff"
    if len(raw) != 8:
        raise AnimateError(f"{text!r} is not a #rrggbb or #rrggbbaa color")
    return np.asarray([int(raw[i:i + 2], 16) / 255.0 for i in range(0, 8, 2)], dtype=np.float64)


def _keys(keys: Keys[Any], base: Timebase, seconds: Floats) -> Floats:
    frames = keys.keys
    times = np.asarray([base.local_seconds(frame.t) for frame in frames], dtype=np.float64)
    values = [_numbers(frame.v) for frame in frames]
    width = values[0].shape[0]
    out = np.empty((seconds.shape[0], width), dtype=np.float64)
    out[:] = values[0]
    for index in range(1, len(frames)):
        frame: Keyframe[Any] = frames[index]
        t0, t1 = times[index - 1], times[index]
        if t1 < t0:
            raise AnimateError(f"keyframe {index} at {t1:.3f}s comes before keyframe {index - 1} at {t0:.3f}s")
        inside = seconds >= t0
        if frame.interp == "hold":
            out[seconds >= t1] = values[index]
            continue
        span = t1 - t0
        u = np.zeros_like(seconds) if span <= 0 else (seconds - t0) / span
        eased = ease(frame.ease, u)[:, None]
        blend = values[index - 1] + (values[index] - values[index - 1]) * eased
        mask = inside & (seconds < t1)
        out[mask] = blend[mask]
        out[seconds >= t1] = values[index]
    return out


def _envelope(envelope: Any, delta: Floats) -> Floats:
    out = np.zeros_like(delta)
    if isinstance(envelope, ExpDecayEnv):
        tau = envelope.tau.ticks / TICKS_PER_SECOND
        on = delta >= 0
        out[on] = np.exp(-delta[on] / max(tau, 1e-9))
    elif isinstance(envelope, LinearEnv):
        rise = envelope.rise.ticks / TICKS_PER_SECOND
        fall = envelope.fall.ticks / TICKS_PER_SECOND
        before = (delta < 0) & (delta >= -rise)
        after = (delta >= 0) & (delta <= fall)
        out[before] = 1.0 + delta[before] / rise
        out[after] = 1.0 - delta[after] / fall
    elif isinstance(envelope, BoxEnv):
        width = envelope.width.ticks / TICKS_PER_SECOND
        out[(delta >= 0) & (delta < width)] = 1.0
    elif isinstance(envelope, EasingEnv):
        duration = envelope.duration.ticks / TICKS_PER_SECOND
        on = (delta >= 0) & (delta < duration)
        eased = ease(envelope.easing, delta[on] / duration)
        out[on] = 1.0 - eased if envelope.direction == "out" else eased
    else:
        raise AnimateError(f"unknown envelope {type(envelope).__name__}")
    return out


def _smooth_noise(rng: np.random.Generator, seconds: Floats, lo: float, hi: float) -> Floats:
    position = np.maximum(seconds, 0.0) / SMOOTH_NOISE_STEP
    index = np.floor(position).astype(np.int64)
    points = rng.uniform(lo, hi, size=int(index.max(initial=0)) + 2)
    frac = position - index
    smooth = frac * frac * (3.0 - 2.0 * frac)
    return points[index] + (points[index + 1] - points[index]) * smooth


def curve_values(curve: CurveBase, base: Timebase, seconds: Floats) -> Floats:
    if isinstance(curve, Sine):
        return curve.offset + curve.amp * np.sin(2.0 * math.pi * curve.freq_hz * seconds + curve.phase)
    if isinstance(curve, LFO):
        phase = 2.0 * math.pi * (curve.freq_hz * seconds + curve.phase)
        if curve.shape == "tri":
            wave = 2.0 / math.pi * np.arcsin(np.sin(phase))
        elif curve.shape == "saw":
            cycles = phase / (2.0 * math.pi)
            wave = 2.0 * (cycles - np.floor(cycles + 0.5))
        else:
            wave = np.sign(np.sin(phase))
        return curve.offset + curve.amp * wave
    if isinstance(curve, ExpDecay):
        tau = curve.tau.ticks / TICKS_PER_SECOND
        return curve.baseline + np.exp(-np.maximum(seconds, 0.0) / tau)
    if isinstance(curve, Lambda):
        function = compile_lambda(curve.expr)
        return np.asarray([function(float(value)) for value in seconds.tolist()], dtype=np.float64)
    if isinstance(curve, Spring):
        return _spring(curve, seconds)
    if isinstance(curve, Noise):
        rng = np.random.default_rng(curve.seed if curve.seed is not None else base.seed)
        if curve.hold is None:
            return _smooth_noise(rng, seconds, curve.min_value, curve.max_value)
        hold = curve.hold.ticks / TICKS_PER_SECOND
        blocks = np.floor(np.maximum(seconds, 0.0) / hold).astype(np.int64)
        samples = rng.uniform(curve.min_value, curve.max_value, size=int(blocks.max(initial=0)) + 1)
        return samples[blocks]
    if isinstance(curve, RandomChoice):
        rng = np.random.default_rng(curve.seed if curve.seed is not None else base.seed)
        hold = curve.hold.ticks / TICKS_PER_SECOND
        blocks = np.floor(np.maximum(seconds, 0.0) / hold).astype(np.int64)
        pool = np.asarray(curve.values, dtype=np.float64)
        picks = pool[rng.integers(0, len(pool), size=int(blocks.max(initial=0)) + 1)]
        return picks[blocks]
    if isinstance(curve, Step):
        hold = curve.hold.ticks / TICKS_PER_SECOND
        pool = np.asarray(curve.values, dtype=np.float64)
        blocks = np.floor(np.maximum(seconds, 0.0) / hold).astype(np.int64)
        return pool[blocks % len(pool)]
    if isinstance(curve, Bezier):
        u = np.clip(seconds / (curve.duration.ticks / TICKS_PER_SECOND), 0.0, 1.0)
        v = 1.0 - u
        return v**3 * curve.p0 + 3.0 * v * v * u * curve.p1 + 3.0 * v * u * u * curve.p2 + u**3 * curve.p3
    if isinstance(curve, Bounce):
        period = curve.period.ticks / TICKS_PER_SECOND
        t = np.maximum(seconds, 0.0)
        return curve.amplitude * np.exp(-curve.decay * t) * np.abs(np.sin(2.0 * math.pi * t / period))
    if isinstance(curve, BeatPulse):
        triggers = base.local_set(curve.trigger)
        intensity = (np.full_like(seconds, float(curve.intensity)) if isinstance(curve.intensity, float | int)
                     else _keys(curve.intensity, base, seconds)[:, 0])
        out = np.full_like(seconds, float(curve.baseline))
        for start in triggers:
            at = int(np.searchsorted(seconds, start)) if seconds.size else 0
            level = float(intensity[min(max(at, 0), max(seconds.size - 1, 0))]) if seconds.size else 0.0
            out += level * _envelope(curve.envelope, seconds - start)
        return out
    hook = getattr(curve, CURVE_HOOK, None)
    if not callable(hook):
        raise AnimateError(f"curve {curve.kind!r} has no {CURVE_HOOK}(seconds, seed) method, so renderers cannot "
                           "evaluate it")
    return np.asarray(hook(seconds, base.seed), dtype=np.float64)


def _spring(curve: Spring, seconds: Floats) -> Floats:
    out = np.empty_like(seconds)
    order = np.argsort(seconds, kind="stable")
    position, velocity, clock = float(curve.initial), 0.0, 0.0
    step = 1.0 / 1000.0
    for index in order.tolist():
        target = max(float(seconds[index]), 0.0)
        while clock + step <= target + 1e-12:
            accel = -curve.stiffness * (position - curve.target) - curve.damping * velocity
            velocity += accel * step
            position += velocity * step
            clock += step
        out[index] = position
    return out


def floats(value: Any, base: Timebase, seconds: Floats) -> Floats:
    if isinstance(value, bool):
        raise AnimateError("a boolean is not an animated number")
    if isinstance(value, int | float):
        return np.full(seconds.shape, float(value), dtype=np.float64)
    if isinstance(value, Keys):
        return _keys(value, base, seconds)[:, 0]
    if isinstance(value, Driven):
        return value.offset + value.scale * curve_values(value.curve, base, seconds)
    raise AnimateError(f"{type(value).__name__} is not an animated number")


def vectors(value: Any, base: Timebase, seconds: Floats) -> Floats:
    if isinstance(value, Keys):
        return _keys(value, base, seconds)
    row = _numbers(value)
    return np.broadcast_to(row, (seconds.shape[0], row.shape[0])).copy()


def colors(value: Any, base: Timebase, seconds: Floats) -> Floats:
    return vectors(value, base, seconds)


def frame_seconds(count: int, fps: float, start: float = 0.0) -> Floats:
    return start + np.arange(count, dtype=np.float64) / fps


__all__ = [
    "EASINGS",
    "AnimateError",
    "Timebase",
    "color_rgba",
    "colors",
    "curve_values",
    "ease",
    "floats",
    "frame_seconds",
    "vectors",
]
