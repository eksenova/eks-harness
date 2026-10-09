"""Easing curves for keyframe interpolation.

Each variant exposes ``evaluate(t01)`` returning the eased ``t`` in [0, 1].
``CubicBezier`` solves for x using Newton-Raphson with bisection fallback,
matching the CSS ``cubic-bezier`` semantics. ``PluginEasing`` is a deferred
reference resolved by the renderer through registered ``EasingPlugin``
implementations.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BackOut",
    "BounceOut",
    "CubicBezier",
    "EaseIn",
    "EaseInCubic",
    "EaseInOut",
    "EaseInOutCubic",
    "EaseOut",
    "EaseOutCubic",
    "Easing",
    "ElasticOut",
    "LinearEasing",
    "PluginEasing",
]


class _EasingBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def evaluate(self, t01: float) -> float:  # pragma: no cover - abstract surface
        raise NotImplementedError


class LinearEasing(_EasingBase):
    kind: Literal["linear"] = "linear"

    def evaluate(self, t01: float) -> float:
        return t01


class EaseIn(_EasingBase):
    kind: Literal["ease_in"] = "ease_in"

    def evaluate(self, t01: float) -> float:
        return t01 * t01


class EaseOut(_EasingBase):
    kind: Literal["ease_out"] = "ease_out"

    def evaluate(self, t01: float) -> float:
        return 1.0 - (1.0 - t01) * (1.0 - t01)


class EaseInOut(_EasingBase):
    kind: Literal["ease_in_out"] = "ease_in_out"

    def evaluate(self, t01: float) -> float:
        if t01 < 0.5:
            return 2.0 * t01 * t01
        return 1.0 - 2.0 * (1.0 - t01) * (1.0 - t01)


class EaseInCubic(_EasingBase):
    kind: Literal["ease_in_cubic"] = "ease_in_cubic"

    def evaluate(self, t01: float) -> float:
        return t01 * t01 * t01


class EaseOutCubic(_EasingBase):
    kind: Literal["ease_out_cubic"] = "ease_out_cubic"

    def evaluate(self, t01: float) -> float:
        u = 1.0 - t01
        return 1.0 - u * u * u


class EaseInOutCubic(_EasingBase):
    kind: Literal["ease_in_out_cubic"] = "ease_in_out_cubic"

    def evaluate(self, t01: float) -> float:
        if t01 < 0.5:
            return 4.0 * t01 * t01 * t01
        u = 1.0 - t01
        return 1.0 - 4.0 * u * u * u


class BounceOut(_EasingBase):
    kind: Literal["bounce_out"] = "bounce_out"

    def evaluate(self, t01: float) -> float:
        n1 = 7.5625
        d1 = 2.75
        if t01 < 1 / d1:
            return n1 * t01 * t01
        if t01 < 2 / d1:
            t01 -= 1.5 / d1
            return n1 * t01 * t01 + 0.75
        if t01 < 2.5 / d1:
            t01 -= 2.25 / d1
            return n1 * t01 * t01 + 0.9375
        t01 -= 2.625 / d1
        return n1 * t01 * t01 + 0.984375


class ElasticOut(_EasingBase):
    kind: Literal["elastic_out"] = "elastic_out"

    def evaluate(self, t01: float) -> float:
        if t01 in (0.0, 1.0):
            return t01
        c4 = (2.0 * math.pi) / 3.0
        return float(2.0 ** (-10.0 * t01) * math.sin((t01 * 10.0 - 0.75) * c4) + 1.0)


class BackOut(_EasingBase):
    kind: Literal["back_out"] = "back_out"

    def evaluate(self, t01: float) -> float:
        c1 = 1.70158
        c3 = c1 + 1.0
        u = t01 - 1.0
        return 1.0 + c3 * u * u * u + c1 * u * u


class CubicBezier(_EasingBase):
    """CSS-style cubic bezier easing with control points in [0, 1]."""

    kind: Literal["cubic_bezier"] = "cubic_bezier"
    p1x: float
    p1y: float
    p2x: float
    p2y: float

    def _bezier_x(self, t: float) -> float:
        return 3.0 * (1 - t) ** 2 * t * self.p1x + 3.0 * (1 - t) * t * t * self.p2x + t**3

    def _bezier_y(self, t: float) -> float:
        return 3.0 * (1 - t) ** 2 * t * self.p1y + 3.0 * (1 - t) * t * t * self.p2y + t**3

    def _dx_dt(self, t: float) -> float:
        return (
            3.0 * (1 - t) ** 2 * self.p1x
            + 6.0 * (1 - t) * t * (self.p2x - self.p1x)
            + 3.0 * t * t * (1.0 - self.p2x)
        )

    def evaluate(self, t01: float) -> float:
        if t01 <= 0.0:
            return 0.0
        if t01 >= 1.0:
            return 1.0
        t = t01
        for _ in range(8):
            x = self._bezier_x(t) - t01
            d = self._dx_dt(t)
            if abs(d) < 1e-9:
                break
            t -= x / d
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
        lo, hi = 0.0, 1.0
        for _ in range(32):
            mid = 0.5 * (lo + hi)
            x = self._bezier_x(mid)
            if abs(x - t01) < 1e-6:
                t = mid
                break
            if x < t01:
                lo = mid
            else:
                hi = mid
            t = mid
        return self._bezier_y(t)


class PluginEasing(_EasingBase):
    """Deferred reference to an ``EasingPlugin`` looked up by name at render time."""

    kind: Literal["plugin"] = "plugin"
    name: str
    params: dict[str, Any] = Field(default_factory=dict)

    def evaluate(self, t01: float) -> float:
        from eks_harness.video.plugins.registry import resolve_easing_plugin

        plugin = resolve_easing_plugin(self.name)
        return plugin.evaluate(t01, self.params)


Easing = Annotated[
    LinearEasing | EaseIn | EaseOut | EaseInOut | EaseInCubic | EaseOutCubic | EaseInOutCubic | BounceOut | ElasticOut | BackOut | CubicBezier | PluginEasing,
    Field(discriminator="kind"),
]
"""Discriminated union of every easing variant."""
