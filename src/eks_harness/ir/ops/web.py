from __future__ import annotations

from typing import Literal

from pydantic import Field

from eks_harness.ir.ops.ui import LEASE, WEB
from eks_harness.ir.registry import Args, op
from eks_harness.ir.target import Target

LogKind = Literal["console", "errors", "network", "websocket"]


@op("web", name="wheel", platforms=WEB, resources=LEASE, location="worker", pinnable=True,
    description="Scroll with the mouse wheel over a target or the page",
    examples=[{"target": {"by": "test_id", "args": {"id": "invoice-list"}}, "dy": 600}])
class Wheel(Args):
    target: Target | None = Field(default=None, description="Element under the pointer; the page when unset")
    dx: int = Field(default=0, description="Horizontal wheel delta in pixels")
    dy: int = Field(default=400, description="Vertical wheel delta in pixels")
    real: bool | None = Field(default=None, description="Use real OS wheel events instead of synthetic ones")


@op("web", name="viewport", platforms=WEB, resources=LEASE, location="worker", pinnable=True,
    description="Switch the page to a viewport preset",
    examples=[{"name": "tablet"}])
class SetViewport(Args):
    name: str = Field(min_length=1, description="Viewport preset such as desktop, phone or tablet")


@op("web", name="logs", out="json", platforms=WEB, resources=LEASE, location="worker", idempotent=True,
    description="Recent console, page error, network or websocket entries",
    examples=[{"kind": "console", "limit": 100}])
class Logs(Args):
    kind: LogKind = Field(default="console", description="Which log to read: console messages, page errors, network requests or websocket frames")
    limit: int = Field(default=100, ge=1, le=2000, description="Largest number of entries returned")


@op("web", name="clear_logs", platforms=WEB, resources=LEASE, location="worker",
    description="Forget buffered log entries",
    examples=[{}])
class ClearLogs(Args):
    pass


__all__ = ["ClearLogs", "LogKind", "Logs", "SetViewport", "Wheel"]
