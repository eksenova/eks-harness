from __future__ import annotations

from pydantic import Field

from eks_harness.ir.refs import RCond
from eks_harness.ir.registry import Args, op


@op("debug", name="break", description="Pause the run here (when the condition holds) for inspection in the debugger",
    examples=[{"label": "before-submit"}])
class Break(Args):
    label: str = Field(min_length=1, max_length=120, description="Name of the breakpoint shown when the run pauses")
    when: RCond | None = Field(default=None, description="Pause only when this condition holds; always when unset")


@op("debug", name="pause_on_error", description="Pause instead of failing when a later node errors",
    examples=[{"on": True}])
class PauseOnError(Args):
    on: bool = Field(default=True, description="Pause the run at the failing node instead of failing it")


__all__ = ["Break", "PauseOnError"]
