from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eks_harness.ir.catalog.easing import Easing
from eks_harness.ir.time import Time

Interp = Literal["linear", "bezier", "hold", "spline"]


class Keyframe[T](BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    t: Time = Field(description="Time of the keyframe; clip-local unless anchored")
    v: T = Field(description="Value at that time")
    ease: Easing | None = Field(default=None, description="Easing from the previous keyframe; linear when unset")
    interp: Interp = Field(default="linear", description="Interpolation from the previous keyframe")


class Keys[T](BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    keys: tuple[Keyframe[T], ...] = Field(min_length=1, description="Keyframes in time order")


__all__ = ["Interp", "Keyframe", "Keys"]
