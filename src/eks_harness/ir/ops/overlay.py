from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.ops.ui import LEASE, UI
from eks_harness.ir.refs import RJson, RMedia, RStr
from eks_harness.ir.registry import Args, Poly, catalog, op
from eks_harness.ir.target import Target
from eks_harness.ir.time import Span

Emphasis = Literal["none", "freeze", "slow"]
Side = Literal["top", "bottom", "left", "right"]
OverlayRenderer = Literal["live", "screenshot", "video"]
IconName = Literal["check", "cross", "warning", "info", "tap", "star", "plus", "minus", "lock", "bell", "arrow-up",
                   "arrow-down", "arrow-left", "arrow-right", "search", "heart"]

BUILTIN = "builtin"
ALL_RENDERERS = {"live": BUILTIN, "screenshot": BUILTIN, "video": BUILTIN}
NO_SCREENSHOT = {"live": BUILTIN, "video": BUILTIN}


class Placement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["auto", "force"] = Field(default="auto", description="auto keeps the overlay on screen and clear of its anchor; force uses side and offsets as given")
    side: Side | None = Field(default=None, description="Side of the anchor to place the overlay on; chosen automatically when unset")
    dx: float = Field(default=0.0, description="Horizontal offset in pixels")
    dy: float = Field(default=0.0, description="Vertical offset in pixels")


class OverlayBase(BaseModel, frozen=True, extra="forbid"):
    kind: str = Field(description="Overlay kind")
    key: str | None = Field(default=None, description="Name used to remove or replace the overlay later")
    window: Span | None = Field(default=None, description="When the overlay is shown; from when it is placed until removed when unset")
    hold: Duration | None = Field(default=None, description="How long the overlay stays when the window has no end")
    keep: bool = Field(default=False, description="Keep the overlay until it is removed, past its window")
    placement: Placement | None = Field(default=None, description="Where the overlay sits relative to its anchor")
    style: str | None = Field(default=None, description="Overlay style preset; the renderer's default when unset")
    emphasis: Emphasis = Field(default="none", description="Freeze or slow the video while the overlay is shown (video renderer)")


@catalog("overlay", renderers=NO_SCREENSHOT, platforms=UI, description="Full-screen title card")
class Title(OverlayBase, frozen=True):
    kind: Literal["title"] = Field(default="title", description="Full-screen title card")
    text: RStr = Field(description="Title text")
    subtitle: RStr | None = Field(default=None, description="Line under the title")
    kicker: RStr | None = Field(default=None, description="Small line above the title")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Caption bar with optional step number")
class Caption(OverlayBase, frozen=True):
    kind: Literal["caption"] = Field(default="caption", description="Caption bar")
    text: RStr = Field(description="Caption text")
    position: Literal["top", "bottom"] = Field(default="bottom", description="Top or bottom of the frame")
    step: int | None = Field(default=None, ge=1, description="Step number shown next to the caption")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Text bubble pointing at a target")
class Callout(OverlayBase, frozen=True):
    kind: Literal["callout"] = Field(default="callout", description="Bubble pointing at an element")
    anchor: Target = Field(description="Element the callout points at")
    text: RStr = Field(description="Callout text")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Dim everything except a target")
class Spotlight(OverlayBase, frozen=True):
    kind: Literal["spotlight"] = Field(default="spotlight", description="Dim everything except an element")
    anchor: Target = Field(description="Element kept bright")
    text: RStr | None = Field(default=None, description="Optional text next to the element")
    dim: float | None = Field(default=None, ge=0.0, le=1.0, description="How dark the rest of the screen gets, from 0 to 1")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Box around a target")
class Highlight(OverlayBase, frozen=True):
    kind: Literal["highlight"] = Field(default="highlight", description="Mark an element")
    anchor: Target = Field(description="Element to mark")
    shape: Literal["box", "outline", "rounded"] = Field(default="box", description="Mark shape")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Arrow from one target to another")
class Arrow(OverlayBase, frozen=True):
    kind: Literal["arrow"] = Field(default="arrow", description="Arrow from one element to another")
    anchor: Target = Field(description="Element the arrow starts at")
    to: Target = Field(description="Element the arrow points at")
    text: RStr | None = Field(default=None, description="Label on the arrow")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=("web",), description="Blur a target")
class Blur(OverlayBase, frozen=True):
    kind: Literal["blur"] = Field(default="blur", description="Blur an element")
    anchor: Target = Field(description="Element to blur")
    radius: int | None = Field(default=None, ge=0, description="Blur radius in pixels; the style's when unset")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Black out a target")
class Redact(OverlayBase, frozen=True):
    kind: Literal["redact"] = Field(default="redact", description="Cover an element with a solid box")
    anchor: Target = Field(description="Element to cover")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Icon next to a target")
class Icon(OverlayBase, frozen=True):
    kind: Literal["icon"] = Field(default="icon", description="Icon next to an element")
    anchor: Target = Field(description="Element the icon sits next to")
    name: IconName = Field(description="Icon name from the overlay icon set")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Uploaded image asset next to a target")
class Image(OverlayBase, frozen=True):
    kind: Literal["image"] = Field(default="image", description="Image next to an element")
    anchor: Target = Field(description="Element the image sits next to")
    image: RMedia = Field(description="The image, a $ref to a media.source node")
    width: int | None = Field(default=None, gt=0, description="Image width in pixels; its own width when unset")


@catalog("overlay", renderers=ALL_RENDERERS, platforms=UI, description="Numbered step badge on a target")
class Step(OverlayBase, frozen=True):
    kind: Literal["step"] = Field(default="step", description="Numbered step badge on an element")
    anchor: Target = Field(description="Element the badge marks")
    index: int | None = Field(default=None, ge=1, description="Step number; counts up from the previous step when unset")


Overlay = Annotated[SerializeAsAny[OverlayBase], Poly("overlay")]


@op("overlay", name="show", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Draw an overlay live in the app while the flow runs",
    examples=[
        {"overlay": {"kind": "title", "text": "Yeni fatura", "hold": "2s"}},
        {"overlay": {"kind": "callout", "anchor": {"by": "test_id", "args": {"id": "total"}}, "text": "Anlık kur"}},
    ])
class Show(Args):
    overlay: Overlay = Field(description="Overlay to draw in the app while recording")


@op("overlay", name="remove", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Remove a kept overlay by key",
    examples=[{"key": "vurgu"}])
class Remove(Args):
    key: str = Field(min_length=1, description="Key of the overlay to remove")


@op("overlay", name="clear", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Remove every live overlay, or every overlay of one kind",
    examples=[{}, {"kind": "caption"}])
class Clear(Args):
    kind: str | None = Field(default=None, description="Remove only overlays of this kind; every overlay when unset")


@op("overlay", name="annotate", out="artifact", pure=True, location="hub",
    description="Render overlays onto a screenshot artifact as a new, re-renderable version",
    examples=[
        {
            "artifact": {"$ref": "n0000000001", "path": []},
            "overlays": [
                {"kind": "callout", "anchor": {"by": "test_id", "args": {"id": "total"}}, "text": "Anlık kur"},
                {"kind": "redact", "anchor": {"by": "test_id", "args": {"id": "tckn"}}},
            ],
            "style": "review",
        },
    ])
class Annotate(Args):
    artifact: RJson = Field(description="Screenshot to annotate: a shot artifact, a checkpoint (its first shot) or "
                                        "an earlier annotation")
    overlays: tuple[Overlay, ...] = Field(min_length=1, max_length=100, description="Overlays drawn on the screenshot")
    style: str | None = Field(default=None, description="Style preset of the annotation")


__all__ = [
    "ALL_RENDERERS",
    "BUILTIN",
    "NO_SCREENSHOT",
    "Annotate",
    "Arrow",
    "Blur",
    "Callout",
    "Caption",
    "Clear",
    "Emphasis",
    "Highlight",
    "Icon",
    "Image",
    "Overlay",
    "OverlayBase",
    "OverlayRenderer",
    "Placement",
    "Redact",
    "Remove",
    "Show",
    "Side",
    "Spotlight",
    "Step",
    "Title",
]
