from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from typing import Any

_NUMBER = r"-?\d+(?:\.\d+)?"
_BASE = re.compile(
    rf"^(?:(?P<sec>{_NUMBER})(?P<unit>s|ms)?|f:(?P<frame>-?\d+)|(?P<grid>beat|downbeat|bar):(?P<index>{_NUMBER})"
    rf"|(?:cue|marker):(?P<cue>[A-Za-z0-9_.:]+(?:-(?!\d)[A-Za-z0-9_.:]+)*)|(?P<end>end)|(?P<start>start))"
)
_OFFSET = re.compile(r"(?P<sign>[+-])\s*(?P<value>\d+(?:\.\d+)?)(?P<unit>s|ms|f|b)")


class TimeRefError(ValueError):
    pass


@dataclass(frozen=True)
class TimeRef:
    text: str
    kind: str
    value: Any
    offsets: tuple[tuple[float, str], ...] = ()

    def __str__(self) -> str:
        return self.text


def parse_time_ref(text: str | float | int) -> TimeRef:
    if isinstance(text, int | float):
        return TimeRef(f"{float(text):g}s", "sec", float(text))
    raw = str(text).strip()
    match = _BASE.match(raw)
    if not match:
        raise TimeRefError(f"'{text}' is not a time (12.5s, 300ms, f:120, beat:8, downbeat:2, bar:4, cue:drop, end)")
    if match["sec"] is not None:
        value = float(match["sec"]) / (1000.0 if match["unit"] == "ms" else 1.0)
        kind, payload = "sec", value
    elif match["frame"] is not None:
        kind, payload = "frame", int(match["frame"])
    elif match["grid"] is not None:
        kind, payload = ("downbeat" if match["grid"] == "bar" else match["grid"]), float(match["index"])
    elif match["cue"] is not None:
        kind, payload = "cue", match["cue"]
    elif match["end"]:
        kind, payload = "end", None
    else:
        kind, payload = "sec", 0.0
    rest = raw[match.end():].replace(" ", "")
    offsets: list[tuple[float, str]] = []
    position = 0
    while position < len(rest):
        found = _OFFSET.match(rest, position)
        if not found:
            raise TimeRefError(f"'{text}': cannot read offset '{rest[position:]}' (use +0.5s, -2f, +1b, +250ms)")
        amount = float(found["value"]) * (-1 if found["sign"] == "-" else 1)
        offsets.append((amount, found["unit"]))
        position = found.end()
    return TimeRef(raw, kind, payload, tuple(offsets))


@dataclass
class TimeContext:
    fps: float
    beats: list[float]
    downbeats: list[float]
    markers: dict[str, float]
    duration: float | None = None

    def beat_time(self, index: float, grid: list[float]) -> float:
        if not grid:
            raise TimeRefError("the score has no beat grid (set clock.tempo.bpm or analyze an audio track)")
        whole = math.floor(index)
        frac = index - whole
        if whole < 0:
            raise TimeRefError(f"beat index {index} is negative")
        if whole >= len(grid) - (1 if frac else 0):
            if len(grid) < 2:
                raise TimeRefError(f"beat index {index} is beyond the grid")
            step = grid[-1] - grid[-2]
            return grid[-1] + (index - (len(grid) - 1)) * step
        if not frac:
            return grid[whole]
        return grid[whole] + frac * (grid[whole + 1] - grid[whole])

    def beat_length_at(self, seconds: float) -> float:
        if len(self.beats) < 2:
            raise TimeRefError("beat offsets need a beat grid")
        i = min(max(bisect.bisect_right(self.beats, seconds) - 1, 0), len(self.beats) - 2)
        return self.beats[i + 1] - self.beats[i]

    def resolve(self, ref: TimeRef | str | float) -> float:
        if not isinstance(ref, TimeRef):
            ref = parse_time_ref(ref)
        if ref.kind == "sec":
            seconds = float(ref.value)
        elif ref.kind == "frame":
            seconds = int(ref.value) / self.fps
        elif ref.kind == "beat":
            seconds = self.beat_time(float(ref.value), self.beats)
        elif ref.kind == "downbeat":
            seconds = self.beat_time(float(ref.value), self.downbeats)
        elif ref.kind == "cue":
            if ref.value not in self.markers:
                raise TimeRefError(f"unknown cue '{ref.value}' (known: {', '.join(sorted(self.markers)) or 'none'})")
            seconds = self.markers[ref.value]
        elif ref.kind == "end":
            if self.duration is None:
                raise TimeRefError("'end' needs a score duration")
            seconds = self.duration
        else:
            raise TimeRefError(f"unknown time kind {ref.kind}")
        for amount, unit in ref.offsets:
            if unit == "s":
                seconds += amount
            elif unit == "ms":
                seconds += amount / 1000.0
            elif unit == "f":
                seconds += amount / self.fps
            else:
                seconds += amount * self.beat_length_at(seconds)
        return seconds

    def frame(self, seconds: float) -> int:
        return int(round(seconds * self.fps))

    def snap(self, seconds: float) -> float:
        return self.frame(seconds) / self.fps


def grid_from_bpm(bpm: float, offset: float, duration: float, beats_per_bar: int) -> tuple[list[float], list[float]]:
    step = 60.0 / bpm
    count = int(math.floor((duration - offset) / step)) + 1 if duration > offset else 1
    beats = [offset + i * step for i in range(max(count, 1))]
    return beats, beats[::beats_per_bar]
