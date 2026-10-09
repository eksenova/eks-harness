from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ERROR_MAX_CHARS = 300


@dataclass(frozen=True)
class StepBox:
    x: float
    y: float
    w: float
    h: float
    scale: float = 1.0

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "scale": self.scale}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepBox":
        return cls(x=float(data["x"]), y=float(data["y"]), w=float(data["w"]),
                   h=float(data["h"]), scale=float(data.get("scale", 1.0)))


@dataclass
class StepEntry:
    t: float
    action: str
    target: dict[str, Any] = field(default_factory=dict)
    page: str = ""
    viewport: str = ""
    ok: bool = True
    error: str | None = None
    ms: int | None = None
    boxes: list[StepBox] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.t, (int, float)) or not math.isfinite(self.t) or self.t < 0:
            raise ValueError(f"step t must be seconds since recording start, not {self.t!r}")
        self.t = float(self.t)
        if not self.action or not str(self.action).strip():
            raise ValueError("step action must be a non-empty string")
        self.action = str(self.action)
        if not isinstance(self.target, dict):
            raise ValueError("step target must be an object")
        if self.error is not None:
            self.error = str(self.error)[:ERROR_MAX_CHARS]
        if self.ms is not None and (isinstance(self.ms, bool) or not isinstance(self.ms, int)):
            raise ValueError("step ms must be an integer")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "t": self.t,
            "action": self.action,
            "target": self.target,
            "page": self.page,
            "ok": self.ok,
        }
        if self.viewport:
            data["viewport"] = self.viewport
        if self.error:
            data["error"] = self.error
        if self.ms is not None:
            data["ms"] = self.ms
        if self.boxes:
            data["boxes"] = [box.to_dict() for box in self.boxes]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepEntry":
        if not isinstance(data, dict):
            raise ValueError("step log row must be an object")
        boxes = [StepBox.from_dict(item) for item in data.get("boxes") or []]
        return cls(t=float(data["t"]), action=str(data["action"]),
                   target=dict(data.get("target") or {}), page=str(data.get("page") or data.get("viewport") or ""),
                   viewport=str(data.get("viewport") or ""), ok=bool(data.get("ok", True)),
                   error=data.get("error"), ms=data.get("ms"), boxes=boxes)


def write_jsonl(path: str | Path, entries: list[StepEntry]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
    return out


def read_jsonl(path: str | Path) -> list[StepEntry]:
    entries: list[StepEntry] = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_no}: not valid JSON") from error
            try:
                entries.append(StepEntry.from_dict(data))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path}:{line_no}: {error}") from error
    entries.sort(key=lambda entry: entry.t)
    return entries


def event_times(entries: list[StepEntry]) -> list[float]:
    return [entry.t for entry in entries]
