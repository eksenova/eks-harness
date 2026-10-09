"""Resolve ``Animated[T]`` declarations into per-frame numpy arrays.

For constant values the original ``T`` is returned. For keyframe sequences
each pair of adjacent keyframes is interpolated over the frames they span,
with the *outgoing* keyframe's easing controlling the curve. For curves the
appropriate evaluator is dispatched.

The :class:`BeatPulse` evaluator is the canary case: it places envelope
shapes at every trigger and sums them, multiplied by an intensity ramp
sampled at each trigger.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, TypeVar

import numpy as np

from eks_harness.video.ir.animated import Animated, Keyframe
from eks_harness.video.ir.curves import (
    LFO,
    BeatPulse,
    Bezier,
    Bounce,
    BoxEnv,
    Curve,
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
)
from eks_harness.video.ir.easing import Easing, LinearEasing
from eks_harness.video.ir.time import BeatRef

from .time_resolve import resolve_time

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project

T = TypeVar("T")


def resolve_animated(
    animated: Animated[Any] | Any,
    project: Project,
    markers: MarkerSet,
    duration_seconds: float | None = None,
) -> np.ndarray | Any:
    """Resolve ``animated`` into a per-frame array or scalar.

    ``duration_seconds`` overrides the project duration for cases where the
    caller wants to materialize only a sub-window (e.g. a single segment).
    """

    if not isinstance(animated, Animated):
        return animated

    duration = duration_seconds if duration_seconds is not None else project.duration
    n_frames = max(1, round(duration * project.fps))

    root = animated.root

    if isinstance(root, list):
        return _resolve_keyframes(root, project, markers, n_frames)
    if _is_curve(root):
        return _resolve_curve(root, project, markers, n_frames)
    return root


def _is_curve(value: Any) -> bool:
    return isinstance(
        value,
        (
            BeatPulse,
            ExpDecay,
            Sine,
            LFO,
            Lambda,
            Spring,
            Noise,
            RandomChoice,
            Step,
            Bezier,
            Bounce,
        ),
    )


def _resolve_keyframes(
    keyframes: list[Keyframe[Any]],
    project: Project,
    markers: MarkerSet,
    n_frames: int,
) -> np.ndarray:
    if not keyframes:
        raise ValueError("Animated keyframe list is empty")

    sorted_kfs = sorted(
        keyframes,
        key=lambda kf: resolve_time(kf.t, project, markers),
    )
    times = np.array([resolve_time(kf.t, project, markers) for kf in sorted_kfs], dtype=np.float64)
    values = np.array([float(kf.v) for kf in sorted_kfs], dtype=np.float64)

    fps = project.fps
    out = np.empty(n_frames, dtype=np.float64)
    frame_seconds = np.arange(n_frames, dtype=np.float64) / fps

    out[frame_seconds <= times[0]] = values[0]
    out[frame_seconds >= times[-1]] = values[-1]

    for i in range(len(sorted_kfs) - 1):
        t_start = times[i]
        t_end = times[i + 1]
        v_start = values[i]
        v_end = values[i + 1]
        easing: Easing = sorted_kfs[i].easing or LinearEasing()
        interp = sorted_kfs[i].interp

        mask = (frame_seconds >= t_start) & (frame_seconds < t_end)
        if not np.any(mask):
            continue
        if interp == "hold":
            out[mask] = v_start
            continue
        span = t_end - t_start
        if span <= 0:
            out[mask] = v_end
            continue
        local = (frame_seconds[mask] - t_start) / span
        eased = np.array([easing.evaluate(float(t)) for t in local], dtype=np.float64)
        out[mask] = v_start + (v_end - v_start) * eased

    return out


def _resolve_curve(
    curve: Curve | Any,
    project: Project,
    markers: MarkerSet,
    n_frames: int,
) -> np.ndarray:
    if isinstance(curve, BeatPulse):
        return _resolve_beat_pulse(curve, project, markers, n_frames)
    if isinstance(curve, ExpDecay):
        return _resolve_exp_decay(curve, project, n_frames)
    if isinstance(curve, Sine):
        return _resolve_sine(curve, project, n_frames)
    if isinstance(curve, LFO):
        return _resolve_lfo(curve, project, n_frames)
    if isinstance(curve, Lambda):
        return _resolve_lambda(curve, project, n_frames)
    if isinstance(curve, Spring):
        return _resolve_spring(curve, project, n_frames)
    if isinstance(curve, Noise):
        return _resolve_noise(curve, project, n_frames)
    if isinstance(curve, RandomChoice):
        return _resolve_random_choice(curve, project, n_frames)
    if isinstance(curve, Step):
        return _resolve_step(curve, project, n_frames)
    if isinstance(curve, Bezier):
        return _resolve_bezier(curve, project, n_frames)
    if isinstance(curve, Bounce):
        return _resolve_bounce(curve, project, n_frames)
    raise TypeError(f"unrecognized Curve variant: {type(curve).__name__}")


def _resolve_beat_pulse(
    pulse: BeatPulse,
    project: Project,
    markers: MarkerSet,
    n_frames: int,
) -> np.ndarray:
    triggers = _expand_beat_triggers(pulse.trigger, project, markers)

    if isinstance(pulse.intensity_ramp, list):
        ramp_anim = Animated[float](root=pulse.intensity_ramp)
        ramp_array = resolve_animated(ramp_anim, project, markers)
        assert isinstance(ramp_array, np.ndarray)
        ramp_per_frame = ramp_array
    else:
        ramp_per_frame = np.full(n_frames, float(pulse.intensity_ramp), dtype=np.float64)

    fps = project.fps
    out = np.full(n_frames, float(pulse.baseline), dtype=np.float64)

    for trigger_t in triggers:
        trigger_frame = round(trigger_t * fps)
        if trigger_frame >= n_frames:
            continue
        intensity = float(ramp_per_frame[max(0, min(trigger_frame, len(ramp_per_frame) - 1))])
        contribution = _evaluate_envelope(pulse.envelope, project, n_frames, trigger_frame)
        out += intensity * contribution

    return out


def _expand_beat_triggers(
    trigger: BeatRef,
    project: Project,
    markers: MarkerSet,
) -> list[float]:
    if trigger.stream not in markers.streams:
        raise LookupError(f"beat stream {trigger.stream!r} not present in extracted markers")
    times = list(markers.streams[trigger.stream])
    if trigger.range is not None:
        lo = resolve_time(trigger.range[0], project, markers)
        hi = resolve_time(trigger.range[1], project, markers)
        times = [t for t in times if lo - 1e-9 <= t <= hi + 1e-9]
    if trigger.every > 1:
        times = times[:: trigger.every]
    if trigger.offset_ms:
        offset = trigger.offset_ms / 1000.0
        times = [t + offset for t in times]
    return times


def _evaluate_envelope(
    envelope: Any,
    project: Project,
    n_frames: int,
    trigger_frame: int,
) -> np.ndarray:
    fps = project.fps
    out = np.zeros(n_frames, dtype=np.float64)

    if isinstance(envelope, ExpDecayEnv):
        tau_frames = max(1e-9, envelope.tau * fps)
        for i in range(trigger_frame, n_frames):
            decay = math.exp(-(i - trigger_frame) / tau_frames)
            if decay < 1e-6:
                break
            out[i] = decay
        return out

    if isinstance(envelope, LinearEnv):
        rise_frames = max(1, round(envelope.rise * fps))
        fall_frames = max(1, round(envelope.fall * fps))
        for offset in range(-rise_frames, fall_frames + 1):
            i = trigger_frame + offset
            if i < 0 or i >= n_frames:
                continue
            if offset <= 0:
                out[i] = 1.0 - (-offset) / rise_frames
            else:
                out[i] = 1.0 - offset / fall_frames
        out = np.clip(out, 0.0, 1.0)
        return out

    if isinstance(envelope, BoxEnv):
        width_frames = max(1, round(envelope.width * fps))
        end = min(n_frames, trigger_frame + width_frames)
        out[trigger_frame:end] = 1.0
        return out

    if isinstance(envelope, EasingEnv):
        # Sample the easing's evaluate(t01) at every frame in the duration
        # window. ``direction="out"`` flips the eased value so the envelope
        # peaks at the trigger and decays toward zero (the usual "pop"
        # feel); ``direction="in"`` keeps the natural forward shape.
        duration_frames = max(1, round(envelope.duration * fps))
        end = min(n_frames, trigger_frame + duration_frames)
        easing = envelope.easing
        for i in range(trigger_frame, end):
            t01 = (i - trigger_frame) / duration_frames
            # Clamp to [0, 1] - defensive against floating-point edges.
            if t01 < 0.0:
                t01 = 0.0
            elif t01 > 1.0:
                t01 = 1.0
            value = easing.evaluate(t01)
            if envelope.direction == "out":
                value = 1.0 - value
            out[i] = value
        return out

    raise TypeError(f"unrecognized Envelope variant: {type(envelope).__name__}")


def _resolve_exp_decay(curve: ExpDecay, project: Project, n_frames: int) -> np.ndarray:
    fps = project.fps
    out = np.full(n_frames, float(curve.baseline), dtype=np.float64)
    tau_frames = max(1e-9, curve.tau * fps)
    for i in range(n_frames):
        out[i] += math.exp(-i / tau_frames)
    return out


def _resolve_sine(curve: Sine, project: Project, n_frames: int) -> np.ndarray:
    fps = project.fps
    t = np.arange(n_frames, dtype=np.float64) / fps
    return curve.offset + curve.amp * np.sin(2.0 * math.pi * curve.freq_hz * t + curve.phase)


def _resolve_lfo(curve: LFO, project: Project, n_frames: int) -> np.ndarray:
    fps = project.fps
    t = np.arange(n_frames, dtype=np.float64) / fps
    phase = curve.phase + 2.0 * math.pi * curve.freq_hz * t
    if curve.shape == "tri":
        wave = 2.0 / math.pi * np.arcsin(np.sin(phase))
    elif curve.shape == "saw":
        wave = 2.0 * (phase / (2.0 * math.pi) - np.floor(phase / (2.0 * math.pi) + 0.5))
    elif curve.shape == "square":
        wave = np.sign(np.sin(phase))
    else:
        raise ValueError(f"unknown LFO shape {curve.shape!r}")
    return curve.offset + curve.amp * wave


def _resolve_lambda(curve: Lambda, project: Project, n_frames: int) -> np.ndarray:
    from asteval import Interpreter  # type: ignore[import-untyped]

    interp = Interpreter(minimal=True)
    fps = project.fps
    out = np.empty(n_frames, dtype=np.float64)
    for i in range(n_frames):
        interp.symtable["t"] = i / fps
        result = interp(curve.expr)
        if interp.error:
            messages = "; ".join(err.get_error()[1] for err in interp.error)
            raise ValueError(f"Lambda evaluation failed: {messages}")
        out[i] = float(result)
    return out


def _project_random_seed(project: Project) -> int:
    """Project-level salt for randomness curves.

    Reads ``project.random_seed`` if defined (a sibling field added by a
    parallel change), otherwise falls back to ``project.metadata['random_seed']``,
    otherwise 0. Tolerant of either shape so curves are reproducible regardless.
    """

    seed = getattr(project, "random_seed", None)
    if seed is not None:
        return int(seed)
    metadata = getattr(project, "metadata", None) or {}
    return int(metadata.get("random_seed", 0))


def _resolve_spring(curve: Spring, project: Project, n_frames: int) -> np.ndarray:
    fps = project.fps
    dt = 1.0 / fps
    out = np.empty(n_frames, dtype=np.float64)
    position = float(curve.initial)
    velocity = 0.0
    for i in range(n_frames):
        out[i] = position
        accel = -curve.stiffness * (position - curve.target) - curve.damping * velocity
        velocity += accel * dt
        position += velocity * dt
    return out


def _resolve_noise(curve: Noise, project: Project, n_frames: int) -> np.ndarray:
    rng = np.random.default_rng(int(curve.seed) ^ _project_random_seed(project))
    lo = float(curve.min_value)
    hi = float(curve.max_value)

    if curve.hold <= 0.0:
        return rng.uniform(lo, hi, size=n_frames).astype(np.float64, copy=False)

    fps = project.fps
    hold_frames = max(1, round(curve.hold * fps))
    n_blocks = (n_frames + hold_frames - 1) // hold_frames
    samples = rng.uniform(lo, hi, size=n_blocks)
    out = np.repeat(samples, hold_frames)[:n_frames]
    return out.astype(np.float64, copy=False)


def _resolve_random_choice(
    curve: RandomChoice, project: Project, n_frames: int
) -> np.ndarray:
    rng = np.random.default_rng(int(curve.seed) ^ _project_random_seed(project))
    fps = project.fps
    hold_frames = max(1, round(curve.hold * fps))
    n_blocks = (n_frames + hold_frames - 1) // hold_frames
    pool = np.array(curve.values, dtype=np.float64)
    picks = pool[rng.integers(0, len(pool), size=n_blocks)]
    out = np.repeat(picks, hold_frames)[:n_frames]
    return out.astype(np.float64, copy=False)


def _resolve_step(curve: Step, project: Project, n_frames: int) -> np.ndarray:
    fps = project.fps
    hold_frames = max(1, round(curve.hold * fps))
    pool = np.array(curve.values, dtype=np.float64)
    block_index = np.arange(n_frames, dtype=np.int64) // hold_frames
    return pool[block_index % len(pool)].astype(np.float64, copy=False)


def _resolve_bezier(curve: Bezier, project: Project, n_frames: int) -> np.ndarray:
    fps = project.fps
    t_seconds = np.arange(n_frames, dtype=np.float64) / fps
    u = np.clip(t_seconds / curve.duration, 0.0, 1.0)
    one_minus = 1.0 - u
    return (
        one_minus**3 * curve.p0
        + 3.0 * one_minus**2 * u * curve.p1
        + 3.0 * one_minus * u**2 * curve.p2
        + u**3 * curve.p3
    ).astype(np.float64, copy=False)


def _resolve_bounce(curve: Bounce, project: Project, n_frames: int) -> np.ndarray:
    fps = project.fps
    t = np.arange(n_frames, dtype=np.float64) / fps
    return (
        curve.amplitude
        * np.exp(-curve.decay * t)
        * np.abs(np.sin(2.0 * math.pi * t / curve.period))
    ).astype(np.float64, copy=False)


__all__ = ["resolve_animated"]
