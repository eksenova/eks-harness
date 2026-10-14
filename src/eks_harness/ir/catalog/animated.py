from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eks_harness.ir.catalog.curves import Curve
from eks_harness.ir.catalog.keys import Keys
from eks_harness.ir.types import Color

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]


class Driven(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    curve: Curve = Field(description="Curve that drives the value over time")
    scale: float = Field(default=1.0, description="Factor applied to the curve's output")
    offset: float = Field(default=0.0, description="Value added after scaling")


AFloat = float | Keys[float] | Driven
AInt = int | Keys[int]
AVec2 = Vec2 | Keys[Vec2]
AVec3 = Vec3 | Keys[Vec3]
AColor = Color | Keys[Color]

__all__ = ["AColor", "AFloat", "AInt", "AVec2", "AVec3", "Driven", "Vec2", "Vec3"]
