from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, SerializeAsAny

from eks_harness.ir.registry import Poly, easing


class EasingBase(BaseModel, frozen=True, extra="forbid"):
    kind: str = Field(description="Easing kind")


@easing(description="constant speed")
class Linear(EasingBase, frozen=True):
    kind: Literal["linear"] = Field(default="linear", description="constant speed")


@easing(description="quadratic ease in")
class InQuad(EasingBase, frozen=True):
    kind: Literal["in_quad"] = Field(default="in_quad", description="quadratic ease in")


@easing(description="quadratic ease out")
class OutQuad(EasingBase, frozen=True):
    kind: Literal["out_quad"] = Field(default="out_quad", description="quadratic ease out")


@easing(description="quadratic ease in and out")
class InOutQuad(EasingBase, frozen=True):
    kind: Literal["in_out_quad"] = Field(default="in_out_quad", description="quadratic ease in and out")


@easing(description="cubic ease in")
class InCubic(EasingBase, frozen=True):
    kind: Literal["in_cubic"] = Field(default="in_cubic", description="cubic ease in")


@easing(description="cubic ease out")
class OutCubic(EasingBase, frozen=True):
    kind: Literal["out_cubic"] = Field(default="out_cubic", description="cubic ease out")


@easing(description="cubic ease in and out")
class InOutCubic(EasingBase, frozen=True):
    kind: Literal["in_out_cubic"] = Field(default="in_out_cubic", description="cubic ease in and out")


@easing(description="quartic ease in")
class InQuart(EasingBase, frozen=True):
    kind: Literal["in_quart"] = Field(default="in_quart", description="quartic ease in")


@easing(description="quartic ease out")
class OutQuart(EasingBase, frozen=True):
    kind: Literal["out_quart"] = Field(default="out_quart", description="quartic ease out")


@easing(description="quartic ease in and out")
class InOutQuart(EasingBase, frozen=True):
    kind: Literal["in_out_quart"] = Field(default="in_out_quart", description="quartic ease in and out")


@easing(description="sine ease in")
class InSine(EasingBase, frozen=True):
    kind: Literal["in_sine"] = Field(default="in_sine", description="sine ease in")


@easing(description="sine ease out")
class OutSine(EasingBase, frozen=True):
    kind: Literal["out_sine"] = Field(default="out_sine", description="sine ease out")


@easing(description="sine ease in and out")
class InOutSine(EasingBase, frozen=True):
    kind: Literal["in_out_sine"] = Field(default="in_out_sine", description="sine ease in and out")


@easing(description="exponential ease in")
class InExpo(EasingBase, frozen=True):
    kind: Literal["in_expo"] = Field(default="in_expo", description="exponential ease in")


@easing(description="exponential ease out")
class OutExpo(EasingBase, frozen=True):
    kind: Literal["out_expo"] = Field(default="out_expo", description="exponential ease out")


@easing(description="exponential ease in and out")
class InOutExpo(EasingBase, frozen=True):
    kind: Literal["in_out_expo"] = Field(default="in_out_expo", description="exponential ease in and out")


@easing(description="bouncing ease out")
class OutBounce(EasingBase, frozen=True):
    kind: Literal["out_bounce"] = Field(default="out_bounce", description="bouncing ease out")


@easing(description="elastic ease out")
class OutElastic(EasingBase, frozen=True):
    kind: Literal["out_elastic"] = Field(default="out_elastic", description="elastic ease out")


@easing(description="ease out with a small overshoot")
class OutBack(EasingBase, frozen=True):
    kind: Literal["out_back"] = Field(default="out_back", description="ease out with a small overshoot")


@easing(description="CSS cubic-bezier(p1x, p1y, p2x, p2y)")
class CubicBezier(EasingBase, frozen=True):
    kind: Literal["cubic_bezier"] = Field(default="cubic_bezier", description="CSS cubic-bezier(p1x, p1y, p2x, p2y)")
    p1x: float = Field(description="x of the first control point, from 0 to 1")
    p1y: float = Field(description="y of the first control point")
    p2x: float = Field(description="x of the second control point, from 0 to 1")
    p2y: float = Field(description="y of the second control point")


Easing = Annotated[SerializeAsAny[EasingBase], Poly("easing")]

LEGACY_NAMES = {
    "ease_in": "in_quad",
    "ease_out": "out_quad",
    "ease_in_out": "in_out_quad",
    "ease_in_cubic": "in_cubic",
    "ease_out_cubic": "out_cubic",
    "ease_in_out_cubic": "in_out_cubic",
    "ease_out_expo": "out_expo",
    "ease_in_expo": "in_expo",
    "ease_in_out_expo": "in_out_expo",
    "bounce_out": "out_bounce",
    "elastic_out": "out_elastic",
    "back_out": "out_back",
}

__all__ = [
    "LEGACY_NAMES",
    "CubicBezier",
    "Easing",
    "EasingBase",
    "InCubic",
    "InExpo",
    "InOutExpo",
    "InOutQuart",
    "InOutSine",
    "InQuart",
    "InSine",
    "OutExpo",
    "OutQuart",
    "OutSine",
    "InOutCubic",
    "InOutQuad",
    "InQuad",
    "Linear",
    "OutBack",
    "OutBounce",
    "OutCubic",
    "OutElastic",
    "OutQuad",
]
