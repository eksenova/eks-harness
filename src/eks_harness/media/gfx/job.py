from __future__ import annotations

import importlib.metadata
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JOB_SCHEMA = "eks/gfx-job@1"
RESULT_SCHEMA = "eks/gfx-result@1"
ACTIVE = "active"
FLOAT_PROPS = frozenset({
    "x", "y", "scale", "rotation", "opacity", "blur", "size", "tracking", "width", "height", "radius",
    "stroke_width", "trim_start", "trim_end",
})
COLOR_PROPS = frozenset({"color", "fill", "stroke"})
PROBE_FLAG = "--probe"
PROGRESS_PREFIX = "frame "
ENCODE_THREADS = 4
DECODE_THREADS = 2


class _Job(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class JobVariant(_Job):
    start: int = Field(ge=0)
    record: dict[str, Any]
    assets: dict[str, Any] = Field(default_factory=dict)


class JobLayer(_Job):
    id: str
    kind: str
    parent: str | None = None
    children: tuple[str, ...] = ()
    variants: tuple[JobVariant, ...]


class GfxJob(_Job):
    schema_: Literal["eks/gfx-job@1"] = Field(default=JOB_SCHEMA, alias="schema")
    size: tuple[int, int]
    fps: str
    frames: int = Field(ge=1)
    background: str | None = None
    cpu: bool
    output: str
    tracks: str
    result: str
    roots: tuple[str, ...]
    layers: tuple[JobLayer, ...]
    encode_threads: int = ENCODE_THREADS
    decode_threads: int = DECODE_THREADS

    @property
    def rate(self) -> Fraction:
        return Fraction(self.fps)


class GfxResult(_Job):
    schema_: Literal["eks/gfx-result@1"] = Field(default=RESULT_SCHEMA, alias="schema")
    ok: bool
    code: str | None = None
    message: str | None = None
    frames: int = 0
    renderer: Literal["cpu", "gpu"] | None = None
    gpu: dict[str, str] | None = None
    skia: str | None = None


def skia_version() -> str:
    try:
        return importlib.metadata.version("skia-python")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def track_key(layer: str, prop: str) -> str:
    return f"{layer}:{prop}"


__all__ = [
    "ACTIVE",
    "COLOR_PROPS",
    "DECODE_THREADS",
    "ENCODE_THREADS",
    "FLOAT_PROPS",
    "JOB_SCHEMA",
    "PROBE_FLAG",
    "PROGRESS_PREFIX",
    "RESULT_SCHEMA",
    "GfxJob",
    "GfxResult",
    "JobLayer",
    "JobVariant",
    "skia_version",
    "track_key",
]
