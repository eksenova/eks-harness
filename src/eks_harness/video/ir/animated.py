"""Animated property values.

``Animated[T]`` accepts three input shapes:

1. A bare ``T`` (constant).
2. A list of ``Keyframe[T]`` interpolated according to each keyframe's easing
   and ``interp`` mode.
3. A ``Curve`` (continuous-time generator).

The ``RootModel`` carries one of those shapes verbatim. Resolution into a
per-frame ``np.ndarray`` happens in :mod:`eks_harness.video.compile.animated_resolve`
once project tempo and markers are known.

Pydantic v2 generics quirk: parameterised aliases like ``Animated[float]``
construct a fresh subclass with the bound type baked into the union. Keep the
inner ``RootModel`` definition lean to keep that subclass cheap to build.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from .curves import Curve
from .easing import Easing, LinearEasing
from .time import TimeRef

__all__ = ["Animated", "Keyframe"]


T = TypeVar("T")


class Keyframe(BaseModel, Generic[T]):
    """Single keyframe in an ``Animated[T]`` sequence."""

    model_config = ConfigDict(extra="forbid")

    t: TimeRef
    v: T
    easing: Easing = Field(default_factory=LinearEasing)
    interp: Literal["linear", "bezier", "hold", "spline"] = "linear"


def _looks_like_curve(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get("kind") in {
        "beat_pulse",
        "exp_decay",
        "sine",
        "lfo",
        "lambda",
        "spring",
        "noise",
        "random_choice",
        "step",
        "bezier",
        "bounce",
    }


def _looks_like_keyframe_list(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    if not value:
        return False
    return all(isinstance(item, dict) and "v" in item and "t" in item for item in value)


class Animated(RootModel[Any], Generic[T]):
    """Wrapper around ``T | list[Keyframe[T]] | Curve``.

    The discriminator is shape-based: dicts with a recognised ``kind`` become
    a ``Curve``, lists of ``{"t","v",...}`` dicts become keyframes, anything
    else is treated as a constant value.
    """

    root: Any

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, Animated):
            return value.root
        return value

    @model_validator(mode="after")
    def _validate_root(self) -> Animated[T]:
        from pydantic import TypeAdapter

        if _looks_like_curve(self.root):
            self.root = TypeAdapter(Curve).validate_python(self.root)
        elif isinstance(self.root, BaseModel):
            pass
        elif _looks_like_keyframe_list(self.root):
            adapter = TypeAdapter(list[Keyframe[Any]])
            self.root = adapter.validate_python(self.root)
        elif isinstance(self.root, list) and self.root and isinstance(self.root[0], Keyframe):
            pass
        return self

    @property
    def value(self) -> Any:
        return self.root

    def is_constant(self) -> bool:
        return not isinstance(self.root, (list, BaseModel))

    def is_curve(self) -> bool:
        return isinstance(self.root, BaseModel) and getattr(self.root, "kind", None) in {
            "beat_pulse",
            "exp_decay",
            "sine",
            "lfo",
            "lambda",
            "spring",
            "noise",
            "random_choice",
            "step",
            "bezier",
            "bounce",
        }

    def is_keyframes(self) -> bool:
        return isinstance(self.root, list)
