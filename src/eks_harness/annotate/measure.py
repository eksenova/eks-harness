from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from eks_harness.annotate.spec import Anchor


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float
    scale: float = 1.0
    viewport_w: int | None = None
    viewport_h: int | None = None
    t: float | None = None

    def edges(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)

    def to_output(self, display_width: int, capture_width: int) -> Box:
        factor = self.scale * (float(display_width) / float(capture_width))
        return Box(x=self.x * factor, y=self.y * factor, w=self.w * factor, h=self.h * factor,
                   scale=self.scale, viewport_w=self.viewport_w, viewport_h=self.viewport_h, t=self.t)

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "scale": self.scale,
                "viewport_w": self.viewport_w, "viewport_h": self.viewport_h, "t": self.t}

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Box:
        return Box(x=float(raw["x"]), y=float(raw["y"]), w=float(raw["w"]), h=float(raw["h"]),
                   scale=float(raw.get("scale", 1.0)), viewport_w=raw.get("viewport_w"),
                   viewport_h=raw.get("viewport_h"), t=raw.get("t"))


@dataclass(frozen=True)
class ResolvedElement:
    box: Box
    text: str = ""
    name: str = ""
    matches: int = 1
    visible: bool = True


class MeasurementProvider(Protocol):
    def resolve(self, anchor: Anchor) -> ResolvedElement:
        ...


class FakeMeasurement:
    def __init__(self, boxes: dict[str, ResolvedElement]) -> None:
        self._boxes = dict(boxes)

    def _key(self, anchor: Anchor) -> str:
        data = anchor.to_dict()
        parts = [data.get("kind", "")]
        for field in ("selector", "role", "name", "text", "testId"):
            if data.get(field) is not None:
                parts.append(f"{field}={data[field]}")
        if anchor.is_coords():
            parts.append(f"coords={anchor.x},{anchor.y},{anchor.width},{anchor.height}")
        return "|".join(parts)

    def resolve(self, anchor: Anchor) -> ResolvedElement:
        if anchor.is_coords():
            box = Box(x=float(anchor.x or 0), y=float(anchor.y or 0),
                      w=float(anchor.width or 0), h=float(anchor.height or 0), scale=1.0)
            return ResolvedElement(box=box, matches=1)
        key = self._key(anchor)
        try:
            return self._boxes[key]
        except KeyError:
            raise LookupError(f"FakeMeasurement has no canned box for {key}.") from None

    @staticmethod
    def box(x: float, y: float, w: float, h: float, *, text: str = "",
            name: str = "", matches: int = 1, scale: float = 1.0) -> ResolvedElement:
        return ResolvedElement(box=Box(x=x, y=y, w=w, h=h, scale=scale),
                               text=text, name=name, matches=matches)


def boxes_match(first: Box, second: Box, tolerance_px: float = 1.0) -> bool:
    return (abs(first.x - second.x) <= tolerance_px
            and abs(first.y - second.y) <= tolerance_px
            and abs(first.w - second.w) <= tolerance_px
            and abs(first.h - second.h) <= tolerance_px)


def stable_measure(provider: MeasurementProvider, anchor: Anchor, *,
                   tolerance_px: float = 1.0,
                   sleep_fn: Callable[[float], None] = time.sleep) -> ResolvedElement:
    if anchor.is_coords():
        return provider.resolve(anchor)
    first = provider.resolve(anchor)
    sleep_fn(0.1)
    second = provider.resolve(anchor)
    if boxes_match(first.box, second.box, tolerance_px):
        return second
    sleep_fn(0.1)
    third = provider.resolve(anchor)
    if boxes_match(second.box, third.box, tolerance_px):
        return third
    raise RuntimeError("layout is not stable across measurements 100 ms apart (retried once).")


def css_to_output_px(css_px: float, scale: float, display_width: int, capture_width: int) -> float:
    return float(css_px) * float(scale) * (float(display_width) / float(capture_width))
