from __future__ import annotations

from pydantic import Field, field_validator

from eks_harness.ir.catalog.durations import Length
from eks_harness.ir.entries import _require_size
from eks_harness.ir.plan import SessionSpec
from eks_harness.ir.registry import Args, op
from eks_harness.ir.time import TICKS_PER_SECOND, Span, Time


@op("score", name="take", out="media", location="worker", resources=("lease",), container=True, bounded=True,
    branches=("body",), exports_body=True,
    description="Run a flow as fast as it settles, record it, and warp the footage so pinned nodes land on their times",
    examples=[
        {
            "session": {
                "key": "take0",
                "app": "mobile-app",
                "platform": "ios",
                "seed": "standard",
                "persona": "harman-admin",
                "now": "2026-01-15T09:00:00+03:00",
            },
            "lead_in": "1s",
        },
    ])
class Take(Args):
    session: SessionSpec = Field(description="Device session of the take: app, platform, seed, persona, backend, now")
    span: Span | None = Field(default=None, description="Part of the score clock the take covers; the whole window when unset")
    lead_in: Length = Field(default=Time(ticks=TICKS_PER_SECOND),
                            description="Footage kept before the first pinned node")
    warp: bool = Field(default=True, description="Retime the footage so every pinned node lands exactly on its time")
    size: tuple[int, int] | None = Field(default=None, description="Frame size of the footage; the device size when unset")
    tolerance: Length | None = Field(
        default=None, description="How far a pinned node may land from its time without warping; one frame when unset")

    @field_validator("size")
    @classmethod
    def _check_size(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        return _require_size(value)


__all__ = ["Take"]
