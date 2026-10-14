from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from numpy.typing import NDArray

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog.effects import Freeze, SpeedRamp
from eks_harness.ir.hashing import derived_seed
from eks_harness.ir.ops.edit import Clip, Timeline, Track
from eks_harness.ir.time import TICKS_PER_SECOND, TimeContext, TimeError
from eks_harness.ir.types import MediaInfo
from eks_harness.media.animate import Timebase, floats

TIMING_EFFECTS = frozenset({"speed_ramp", "freeze"})
MAX_CLIP_FRAMES = 30 * 60 * 120


def frame_of(ticks: int, fps: Fraction) -> int:
    return round(Fraction(ticks) * fps / TICKS_PER_SECOND)


def ticks_of(frame: int, fps: Fraction) -> int:
    return round(Fraction(frame) * TICKS_PER_SECOND / fps)


def _resolve(context: TimeContext, time: object, where: str) -> int:
    try:
        return context.resolve(time)  # type: ignore[arg-type]
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"{where}: {error}") from error


@dataclass(frozen=True)
class ClipTiming:
    clip: Clip
    track: Track
    track_index: int
    clip_index: int
    first: int
    count: int
    source: NDArray[np.float64]
    source_start: float
    seed: int
    base: Timebase

    @property
    def end(self) -> int:
        return self.first + self.count

    def covers(self, frame: int) -> bool:
        return self.first <= frame < self.end


def _source_span(clip: Clip, info: MediaInfo, context: TimeContext) -> tuple[float, float | None]:
    if clip.src is None:
        length = info.duration.ticks / TICKS_PER_SECOND if info.duration is not None and info.kind != "image" else None
        return 0.0, length
    start = _resolve(context, clip.src.start, f"clip {clip.id} src start")
    end = _resolve(context, clip.src.end, f"clip {clip.id} src end")
    if end <= start:
        raise NodeFailure("time_unresolved", f"clip {clip.id}: its src span ends before it starts")
    return start / TICKS_PER_SECOND, (end - start) / TICKS_PER_SECOND


def _speeds(clip: Clip, base: Timebase, local: NDArray[np.float64]) -> NDArray[np.float64]:
    speed = floats(clip.speed, base, local)
    for effect in clip.effects:
        if isinstance(effect, SpeedRamp):
            speed = speed * floats(effect.factor, base, local)
    if np.any(speed <= 0):
        raise NodeFailure("args_invalid", f"clip {clip.id}: speed must stay above 0, it reaches {float(speed.min())}")
    return speed


def _freezes(clip: Clip, base: Timebase) -> list[tuple[float, float]]:
    found: list[tuple[float, float]] = []
    for effect in clip.effects:
        if isinstance(effect, Freeze):
            at = base.local_seconds(effect.at)
            hold = effect.hold.ticks / TICKS_PER_SECOND
            found.append((at, hold))
    return sorted(found)


def source_map(clip: Clip, base: Timebase, fps: Fraction, length: float | None, limit: int,
               ) -> NDArray[np.float64]:
    rate = float(fps)
    horizon = limit
    local = np.arange(horizon, dtype=np.float64) / rate
    speed = _speeds(clip, base, local)
    freezes = _freezes(clip, base)
    step = speed / rate
    for at, hold in freezes:
        stopped = (local >= at) & (local < at + hold)
        step = np.where(stopped, 0.0, step)
    position = np.concatenate(([0.0], np.cumsum(step)[:-1])) if horizon else np.zeros(0)
    if length is not None:
        inside = position < length - 1e-9
        count = int(np.argmin(inside)) if not bool(inside.all()) else horizon
        position = position[:count]
    return position


def base_for(context: TimeContext, origin: int, seed: int, path: str) -> Timebase:
    return Timebase(context=context, origin=origin, length=None, seed=seed, path=path)


def _horizon(clip: Clip, base: Timebase, fps: Fraction, length: float) -> int:
    ramps = any(effect.kind == "speed_ramp" for effect in clip.effects)
    if isinstance(clip.speed, int | float) and not ramps:
        held = sum(hold for _at, hold in _freezes(clip, base))
        return min(MAX_CLIP_FRAMES, int((length / float(clip.speed) + held) * float(fps)) + 2)
    return MAX_CLIP_FRAMES


def plan_clips(timeline: Timeline, context: TimeContext, infos: dict[str, MediaInfo],
               duration_hint: int | None) -> tuple[list[ClipTiming], int]:
    fps = Fraction(timeline.fps)
    explicit = _resolve(context, timeline.duration, "timeline duration") if timeline.duration is not None else None
    total_hint = frame_of(explicit, fps) if explicit is not None else duration_hint
    timings: list[ClipTiming] = []
    for track_index, track in enumerate(timeline.tracks):
        starts = [frame_of(_resolve(context, clip.start, f"clip {clip.id} start"), fps) for clip in track.clips]
        for clip_index, clip in enumerate(track.clips):
            info = infos[clip.id]
            first = starts[clip_index]
            if first < 0:
                raise NodeFailure("time_unresolved", f"clip {clip.id} starts before the timeline")
            seed = derived_seed(timeline.seed, clip.id)
            origin = ticks_of(first, fps)
            src_start, length = _source_span(clip, info, context)
            following = [value for value in starts[clip_index + 1:] if value > first]
            if length is None:
                stop = min(following) if following else total_hint
                if stop is None:
                    raise NodeFailure("args_invalid", f"clip {clip.id} shows a still image without src; give it a "
                                      "src span, a following clip or a timeline duration")
                limit = stop - first
            else:
                limit = _horizon(clip, base_for(context, origin, seed, clip.id), fps, length)
                if total_hint is not None:
                    limit = min(limit, max(total_hint - first, 0))
            base = Timebase(context=context, origin=origin, length=None, seed=seed, path=clip.id)
            positions = source_map(clip, base, fps, length, limit) if length is not None else np.zeros(limit)
            if length is not None and positions.size >= MAX_CLIP_FRAMES:
                raise NodeFailure("args_invalid", f"clip {clip.id} would run longer than two hours")
            count = int(positions.size)
            base = Timebase(context=context, origin=origin, length=ticks_of(count, fps), seed=seed, path=clip.id)
            timings.append(ClipTiming(clip=clip, track=track, track_index=track_index, clip_index=clip_index,
                                      first=first, count=count, source=src_start + positions,
                                      source_start=src_start, seed=seed, base=base))
    end = max((timing.end for timing in timings), default=0)
    total = total_hint if total_hint is not None else end
    if total <= 0:
        raise NodeFailure("args_invalid", "the timeline is empty: give it clips or a duration")
    return timings, total


def neighbours(timings: Sequence[ClipTiming], timing: ClipTiming) -> tuple[ClipTiming | None, ClipTiming | None]:
    same = sorted((item for item in timings if item.track_index == timing.track_index),
                  key=lambda item: (item.first, item.clip_index))
    index = same.index(timing)
    return (same[index - 1] if index > 0 else None, same[index + 1] if index + 1 < len(same) else None)


__all__ = ["TIMING_EFFECTS", "ClipTiming", "frame_of", "neighbours", "plan_clips", "source_map", "ticks_of"]
