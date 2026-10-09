"""Symbolic time references.

Every animatable property and segment boundary in the video engine is anchored to a
``TimeRef``. The renderer resolves these into concrete frame indices once
markers (beats, words, scenes) have been extracted.

A compact string shorthand is supported on input so users can write::

    "0:16.5"     -> Seconds(t=16.5)        (mm:ss.ms)
    "f:480"      -> Frames(n=480)
    "b:kick:4"   -> BeatRef(stream="kick", every=4)
    "w:hello"    -> WordRef(text="hello")
    "m:intro"    -> MarkerRef(name="intro")
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, model_validator

__all__ = [
    "BeatRef",
    "Frames",
    "MarkerRef",
    "Seconds",
    "TimeRef",
    "TimeRefAdapter",
    "WordRef",
    "parse_time_string",
]


class _TimeBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Seconds(_TimeBase):
    """Absolute time in seconds from project start."""

    kind: Literal["seconds"] = "seconds"
    t: float


class Frames(_TimeBase):
    """Absolute time in frames at the project frame rate."""

    kind: Literal["frames"] = "frames"
    n: int


class BeatRef(_TimeBase):
    """Reference to a beat marker stream emitted by a ``MarkerSource``.

    ``every=N`` selects every N-th beat in the stream. ``offset_ms`` shifts
    the resolved time. ``range`` optionally bounds resolution to a sub-window
    of the project.
    """

    kind: Literal["beat"] = "beat"
    stream: str
    every: int = 1
    offset_ms: float = 0.0
    range: tuple[TimeRef, TimeRef] | None = None


class WordRef(_TimeBase):
    """Reference to a word or word-index marker emitted by an STT marker source."""

    kind: Literal["word"] = "word"
    text: str | None = None
    index: int | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _require_text_or_index(self) -> WordRef:
        if self.text is None and self.index is None:
            raise ValueError("WordRef requires either 'text' or 'index'")
        return self


class MarkerRef(_TimeBase):
    """Reference to a named marker (e.g. scene cut)."""

    kind: Literal["marker"] = "marker"
    name: str
    index: int | None = None


def _parse_mmss(spec: str) -> Seconds:
    parts = spec.split(":")
    if len(parts) == 1:
        return Seconds(t=float(parts[0]))
    if len(parts) == 2:
        minutes, seconds = parts
        return Seconds(t=int(minutes) * 60 + float(seconds))
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return Seconds(t=int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    raise ValueError(f"unrecognized time string: {spec!r}")


def parse_time_string(spec: str) -> Seconds | Frames | BeatRef | WordRef | MarkerRef:
    """Parse the compact ``TimeRef`` shorthand into a concrete model.

    Accepted forms::

        "0:16.5"          -> Seconds(t=16.5)
        "f:480"           -> Frames(n=480)
        "b:<stream>"      -> BeatRef(stream)
        "b:<stream>:<n>"  -> BeatRef(stream, every=n)
        "w:<text>"        -> WordRef(text)
        "w:#<index>"      -> WordRef(index=int)
        "m:<name>"        -> MarkerRef(name)
    """

    if not spec:
        raise ValueError("empty time string")

    if spec.startswith("f:"):
        return Frames(n=int(spec[2:]))
    if spec.startswith("b:"):
        rest = spec[2:]
        bits = rest.split(":")
        if len(bits) == 1:
            return BeatRef(stream=bits[0])
        if len(bits) == 2:
            return BeatRef(stream=bits[0], every=int(bits[1]))
        raise ValueError(f"too many fields in beat reference: {spec!r}")
    if spec.startswith("w:"):
        rest = spec[2:]
        if rest.startswith("#"):
            return WordRef(index=int(rest[1:]))
        return WordRef(text=rest)
    if spec.startswith("m:"):
        return MarkerRef(name=spec[2:])
    return _parse_mmss(spec)


def _coerce_time_ref(value: Any) -> Any:
    if isinstance(value, str):
        return parse_time_string(value).model_dump()
    return value


TimeRef = Annotated[
    Seconds | Frames | BeatRef | WordRef | MarkerRef,
    Field(discriminator="kind"),
    BeforeValidator(_coerce_time_ref),
]
"""Discriminated union of every concrete ``TimeRef`` kind.

Strings are auto-coerced via the ``"0:16"`` / ``"f:480"`` / ``"b:kick:4"``
shorthand documented in :func:`parse_time_string`.
"""


TimeRefAdapter: TypeAdapter[Seconds | Frames | BeatRef | WordRef | MarkerRef] = TypeAdapter(TimeRef)


BeatRef.model_rebuild()
