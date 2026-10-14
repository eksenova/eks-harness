from __future__ import annotations

from typing import Literal

from pydantic import Field

from eks_harness.ir.ops.ui import LEASE, UI, WEB
from eks_harness.ir.refs import RJson, RStr
from eks_harness.ir.registry import Args, op

When = Literal["init", "now"]


@op("inject", name="script", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Run JavaScript now, or on every page load before the app's own scripts (init, web only)",
    examples=[{"source": "window.__flags = {beta: true}", "when": "init"}])
class Script(Args):
    source: RStr = Field(description="JavaScript source to run in the page")
    when: When = Field(default="now", description="Run now, or before every page load (init)")


@op("inject", name="style", platforms=WEB, resources=LEASE, location="worker", pinnable=True,
    description="Add a stylesheet to the page",
    examples=[{"css": "[data-testid='clock'] { visibility: hidden; }"}])
class Style(Args):
    css: RStr = Field(description="CSS injected into the page")


@op("inject", name="eval", out="json", platforms=UI, resources=LEASE, location="worker",
    description="Evaluate JavaScript in the page or the React Native runtime and return the JSON result",
    examples=[{"source": "() => window.__flags"}])
class Eval(Args):
    source: RStr = Field(description="JavaScript expression or function body evaluated in the page or app")
    arg: RJson = Field(default=None, description="JSON value passed to the code as its argument")


__all__ = ["Eval", "Script", "Style", "When"]
