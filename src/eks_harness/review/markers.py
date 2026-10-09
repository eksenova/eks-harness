from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FRAME_KINDS = frozenset({"cue", "frame"})


@dataclass(frozen=True)
class Marker:
    t: float
    kind: str = "cue"
    label: str = ""

    def as_dict(self) -> dict:
        return {"t": self.t, "kind": self.kind, "label": self.label}


@dataclass
class Markers:
    ticks: list[Marker]
    frames: list[Marker]

    def as_dict(self) -> dict:
        return {"markers": [m.as_dict() for m in self.ticks], "frames": [m.as_dict() for m in self.frames]}

    def extend(self, other: "Markers") -> "Markers":
        return Markers(ticks=self.ticks + other.ticks, frames=self.frames + other.frames)


def _time(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"not a time: {value!r}")
    return float(value)


def _marker(value: Any, kind: str) -> Marker:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Marker(t=float(value), kind=kind)
    if isinstance(value, dict):
        at = value.get("t", value.get("at", value.get("time")))
        if at is None:
            raise ValueError(f"marker without t: {value!r}")
        label = value.get("label") or value.get("name") or value.get("text") or ""
        return Marker(t=_time(at), kind=str(value.get("kind") or kind), label=str(label))
    raise ValueError(f"not a marker: {value!r}")


def parse_markers(data: Any) -> Markers:
    ticks: list[Marker] = []
    frames: list[Marker] = []
    if data is None:
        return Markers([], [])
    if isinstance(data, list):
        for item in data:
            marker = _marker(item, "marker")
            ticks.append(marker)
            if marker.kind in FRAME_KINDS or (isinstance(item, dict) and item.get("frame")):
                frames.append(marker)
        return Markers(ticks, frames)
    if not isinstance(data, dict):
        raise ValueError("markers must be a list or an object")
    for key, kind in (("beats", "beat"), ("downbeats", "downbeat")):
        for item in data.get(key) or []:
            ticks.append(_marker(item, kind))
    for item in data.get("cues") or []:
        marker = _marker(item, "cue")
        ticks.append(marker)
        frames.append(marker)
    nested = parse_markers(data["markers"]) if "markers" in data else Markers([], [])
    ticks.extend(nested.ticks)
    frames.extend(nested.frames)
    for item in data.get("frames") or []:
        frames.append(_marker(item, "frame"))
    return Markers(ticks, frames)


def load_markers(path: Path | str) -> Markers:
    return parse_markers(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_at(values: list[str]) -> list[Marker]:
    out = []
    for value in values:
        at, _, label = value.partition(":")
        out.append(Marker(t=float(at), kind="frame", label=label))
    return out
