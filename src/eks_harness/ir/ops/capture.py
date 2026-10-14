from __future__ import annotations

from typing import Literal

from pydantic import Field

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.ops.ui import LEASE, UI, WEB
from eks_harness.ir.presets import Pace
from eks_harness.ir.refs import RStr
from eks_harness.ir.registry import Args, op
from eks_harness.ir.target import Target

DumpKind = Literal["dom", "a11y", "mhtml"]


@op("capture", name="shot", out="artifact", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Screenshot of the screen, a target element or the full page",
    examples=[
        {"name": "fatura-ozet", "full": True},
        {"name": "toplam", "element": {"by": "test_id", "args": {"id": "total"}}},
    ])
class Shot(Args):
    name: RStr = Field(description="Name of the screenshot artifact")
    element: Target | None = Field(default=None, description="Element to crop to; the whole screen when unset")
    focus: Target | None = Field(default=None, description="Element to scroll into view before the shot")
    full: bool = Field(default=False, description="Capture the full scrollable page on web")
    viewport: str | None = Field(default=None, description="Viewport preset to switch to for the shot")
    hide: tuple[Target, ...] = Field(default=(), description="Elements hidden while the shot is taken")


@op("capture", name="dump", out="artifact", platforms=WEB, resources=LEASE, location="worker",
    description="DOM, accessibility tree or MHTML snapshot",
    examples=[{"kind": "dom"}, {"kind": "a11y", "viewport": "phone"}])
class Dump(Args):
    kind: DumpKind = Field(description="What to snapshot: dom, a11y (accessibility tree) or mhtml")
    name: RStr | None = Field(default=None, description="Name of the artifact; derived from the kind when unset")
    viewport: str | None = Field(default=None, description="Viewport preset to switch to for the snapshot")


@op("capture", name="har", out="artifact", platforms=WEB, resources=LEASE, location="worker",
    description="HAR of the requests since the last har() or the whole session",
    examples=[{"whole": True}])
class Har(Args):
    name: RStr | None = Field(default=None, description="Name of the HAR artifact")
    whole: bool = Field(default=False, description="Include every request of the session, not only those since the last HAR")


@op("capture", name="record", out="artifact", platforms=UI, resources=LEASE, location="worker",
    container=True, branches=("body",), exports_body=True,
    description="Record the screen while the body runs, with pacing and a visible pointer",
    examples=[{"name": "fatura-anlatim", "pace": "demo", "pointer": True}])
class Record(Args):
    name: RStr = Field(description="Name of the video artifact")
    pace: str | Pace | None = Field(default=None, description="Pace preset name or inline pace; the demo pace when unset")
    viewport: str | None = Field(default=None, description="Viewport preset for the recording")
    pointer: bool = Field(default=True, description="Draw a visible pointer that moves to each target")
    from_here: bool = Field(default=True, description="Start the video at the current screen instead of reloading it")
    focus: Target | None = Field(default=None, description="Element to keep framed while recording")
    trim: bool | None = Field(default=None, description="Trim idle frames at the start and end; the profile default when unset")
    hide: tuple[Target, ...] = Field(default=(), description="Elements hidden during the recording")


@op("capture", name="hold", platforms=UI, resources=LEASE, location="worker", pinnable=True, recording_only=True,
    description="Keep the current frame on screen for a while; only inside a recording or a take",
    examples=[{"duration": "1s"}])
class Hold(Args):
    duration: Duration = Field(description="How long the current frame stays on screen")
    reason: str | None = Field(default=None, max_length=200, description="Why the frame is held, shown in the journal")


__all__ = ["Dump", "DumpKind", "Har", "Hold", "Record", "Shot"]
