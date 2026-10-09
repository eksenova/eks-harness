from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from eks_harness.capture.steplog import read_jsonl

KEEP_BEFORE_SEC = 1.2
KEEP_AFTER_SEC = 2.0
MAX_SPEEDUP = 4.0
MIN_SCREEN_SEC = 2.0
FPS = 30


@dataclass(frozen=True)
class TrimConfig:
    keep_before_sec: float = KEEP_BEFORE_SEC
    keep_after_sec: float = KEEP_AFTER_SEC
    max_speedup: float = MAX_SPEEDUP
    min_screen_sec: float = MIN_SCREEN_SEC

    def __post_init__(self) -> None:
        for name in ("keep_before_sec", "keep_after_sec", "min_screen_sec"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number of seconds")
        if not isinstance(self.max_speedup, (int, float)) or self.max_speedup < 1:
            raise ValueError("max_speedup must be at least 1")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TrimConfig":
        source = os.environ if env is None else env

        def number(name: str, default: float) -> float:
            raw = source.get(name, "")
            if raw in (None, ""):
                return default
            try:
                return float(str(raw).strip())
            except ValueError as error:
                raise ValueError(f"{name} must be a number, not {raw!r}") from error

        return cls(
            keep_before_sec=number("EKS_TRIM_KEEP_BEFORE", KEEP_BEFORE_SEC),
            keep_after_sec=number("EKS_TRIM_KEEP_AFTER", KEEP_AFTER_SEC),
            max_speedup=number("EKS_TRIM_MAX_SPEEDUP", MAX_SPEEDUP),
            min_screen_sec=number("EKS_TRIM_MIN_SCREEN", MIN_SCREEN_SEC),
        )


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    speed: float = 1.0

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"segment ends before it starts: {self.start} to {self.end}")
        if self.speed < 1:
            raise ValueError(f"segment speed must be at least 1, not {self.speed}")

    @property
    def input_seconds(self) -> float:
        return self.end - self.start

    @property
    def output_seconds(self) -> float:
        return self.input_seconds / self.speed


def _windows(duration: float, events: list[float], config: TrimConfig) -> list[tuple[float, float]]:
    windows: list[tuple[float, float]] = []
    for at in sorted(events):
        if at < 0 or at > duration:
            continue
        start = max(0.0, at - config.keep_before_sec)
        end = min(duration, at + config.keep_after_sec)
        short = config.min_screen_sec - (end - start)
        if short > 0:
            grow_start = min(short, start)
            start -= grow_start
            short -= grow_start
            end = min(duration, end + short)
        windows.append((round(start, 3), round(end, 3)))
    merged: list[list[float]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    floored: list[list[float]] = []
    for start, end in merged:
        if floored and start - floored[-1][1] < config.min_screen_sec:
            floored[-1][1] = end
        else:
            floored.append([start, end])
    return [(round(start, 3), round(end, 3)) for start, end in floored]


def plan(duration: float, events: list[float], config: TrimConfig | None = None) -> list[Segment]:
    config = config or TrimConfig()
    if duration <= 0:
        return []
    if duration <= config.min_screen_sec or not [at for at in events if 0 <= at <= duration]:
        return [Segment(0.0, round(duration, 3), 1.0)]
    windows = _windows(duration, events, config)
    segments: list[Segment] = []
    cursor = 0.0
    for start, end in windows:
        if start > cursor:
            gap = round(start - cursor, 3)
            speed = min(config.max_speedup, gap / config.min_screen_sec)
            segments.append(Segment(round(cursor, 3), start, round(max(1.0, speed), 3)))
        segments.append(Segment(start, end, 1.0))
        cursor = end
    if cursor < duration:
        gap = round(duration - cursor, 3)
        speed = min(config.max_speedup, gap / config.min_screen_sec)
        segments.append(Segment(round(cursor, 3), round(duration, 3), round(max(1.0, speed), 3)))
    return [seg for seg in segments if seg.input_seconds > 0]


def expected_duration(segments: list[Segment]) -> float:
    return round(sum(seg.output_seconds for seg in segments), 3)


def output_offset(segments: list[Segment], at: float) -> float | None:
    elapsed = 0.0
    for seg in segments:
        if seg.start <= at <= seg.end:
            return round(elapsed + (at - seg.start) / seg.speed, 3)
        elapsed += seg.output_seconds
    return None


def filter_graph(segments: list[Segment], fps: int = FPS) -> str:
    if not segments:
        raise ValueError("no segments to encode")
    parts: list[str] = []
    labels: list[str] = []
    for index, seg in enumerate(segments):
        label = f"[seg{index}]"
        labels.append(label)
        chain = f"[0:v]trim=start={seg.start:.3f}:end={seg.end:.3f},setpts=PTS-STARTPTS"
        if seg.speed != 1.0:
            chain += f",setpts=PTS/{seg.speed:g}"
        parts.append(chain + label)
    if len(segments) == 1:
        parts.append(f"{labels[0]}fps={fps}[outv]")
    else:
        parts.append("".join(labels) + f"concat=n={len(segments)}:v=1:a=0,fps={fps}[outv]")
    return ";".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan a step-log trim filter for ffmpeg")
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--steps", default="")
    parser.add_argument("--events", default="")
    parser.add_argument("--keep-before", type=float, default=None)
    parser.add_argument("--keep-after", type=float, default=None)
    parser.add_argument("--max-speedup", type=float, default=None)
    parser.add_argument("--min-screen", type=float, default=None)
    parser.add_argument("--fps", type=int, default=FPS)
    args = parser.parse_args(argv)
    config = TrimConfig.from_env()
    overrides = {
        "keep_before_sec": args.keep_before,
        "keep_after_sec": args.keep_after,
        "max_speedup": args.max_speedup,
        "min_screen_sec": args.min_screen,
    }
    values = {key: getattr(config, key) for key in
              ("keep_before_sec", "keep_after_sec", "max_speedup", "min_screen_sec")}
    values.update({key: value for key, value in overrides.items() if value is not None})
    config = TrimConfig(**values)
    events: list[float] = []
    if args.steps:
        try:
            events = [entry.t for entry in read_jsonl(args.steps)]
        except (OSError, ValueError) as error:
            print(f"step log unreadable, encoder falls back to pixel trim: {error}", file=sys.stderr)
            return 2
    if args.events:
        events += [float(part) for part in args.events.split(",") if part.strip()]
    segments = plan(args.duration, events, config)
    if not segments:
        return 2
    sys.stdout.write(filter_graph(segments, args.fps) + "\n")
    print(f"trim plan: {len(segments)} segments, {args.duration:.1f}s in, "
          f"{expected_duration(segments):.1f}s out", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
