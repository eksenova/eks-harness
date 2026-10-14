from __future__ import annotations

from typing import Literal

from pydantic import Field

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.refs import RJson, RStr
from eks_harness.ir.registry import Args, op
from eks_harness.ir.target import Target

UI = ("web", "ios", "android")
WEB = ("web",)
MOBILE = ("ios", "android")
LEASE = ("lease",)

ScrollEdge = Literal["top", "bottom", "start", "end"]
SwipeDirection = Literal["up", "down", "left", "right"]
WaitState = Literal["visible", "attached", "hidden", "enabled"]


class Act(Args):
    timeout: Duration | None = Field(
        default=None, description="How long to wait for the target to match and become actionable; the session default when unset")
    real: bool | None = Field(
        default=None, description="Use real OS input events instead of synthetic ones; the driver default when unset")
    settle: bool = Field(default=True, description="Wait until the app settles after the action")


@op("ui", name="nav", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Navigate to a web path or a React Navigation route with params",
    examples=[{"route": "/tenant/settings/branches"}, {"route": "InvoiceDetail", "params": {"id": 42}}])
class Nav(Args):
    route: RStr = Field(description="Web path such as /tenant/settings or a React Navigation route name")
    params: dict[str, RJson] | None = Field(
        default=None, description="Route params on React Native, query parameters on web")
    timeout: Duration | None = Field(default=None, description='How long to wait for the navigation to finish; the session default when unset')


@op("ui", name="back", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Go back one screen or history entry",
    examples=[{}])
class Back(Args):
    timeout: Duration | None = Field(default=None, description='How long to wait for the navigation to finish; the session default when unset')


@op("ui", name="reload", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Reload the page or the app bundle",
    examples=[{}])
class Reload(Args):
    timeout: Duration | None = Field(
        default=None, description="How long to wait for the reload to finish; the session default when unset")


@op("ui", name="open_url", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Open an absolute URL in the browser or through the device",
    examples=[{"url": "http://localhost:3000/tenant/settings/branches"}])
class OpenUrl(Args):
    url: RStr = Field(description="Absolute URL to open")
    timeout: Duration | None = Field(default=None, description='How long to wait for the navigation to finish; the session default when unset')


@op("ui", name="deeplink", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Open a deep link in the app",
    examples=[{"url": "eksengold://invoice/new"}])
class Deeplink(Args):
    url: RStr = Field(description="Deep link URL in the app's scheme")
    timeout: Duration | None = Field(default=None, description='How long to wait for the navigation to finish; the session default when unset')


@op("ui", name="tap", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Tap or click a target",
    examples=[{"target": {"by": "test_id", "args": {"id": "credit-add-submit"}}}])
class Tap(Act):
    target: Target = Field(description="Element to tap")
    force: bool = Field(default=False, description="Skip the actionability checks (visible, enabled, not covered)")


@op("ui", name="double_tap", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Double tap or double click",
    examples=[{"target": {"by": "test_id", "args": {"id": "invoice-row"}, "nth": 0}}])
class DoubleTap(Act):
    target: Target = Field(description="Element to double tap")


@op("ui", name="long_press", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Press and hold a target",
    examples=[{"target": {"by": "test_id", "args": {"id": "invoice-row"}}, "hold": "800ms"}])
class LongPress(Act):
    target: Target = Field(description="Element to press")
    hold: Duration | None = Field(
        default=None, description="How long to hold the press; the platform's long press length when unset")


@op("ui", name="hover", platforms=WEB, resources=LEASE, location="worker", pinnable=True, description="Hover a target",
    examples=[{"target": {"by": "role", "args": {"role": "button", "name": "Filtrele"}}}])
class Hover(Act):
    target: Target = Field(description="Element to move the pointer over")


@op("ui", name="fill", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Replace the value of an input",
    examples=[{"target": {"by": "test_id", "args": {"id": "amount"}}, "value": "2450.10"}])
class Fill(Act):
    target: Target = Field(description="Input to fill")
    value: RStr = Field(description="Text that replaces the input's value")
    submit: bool = Field(default=False, description="Press Enter or the keyboard's submit key afterwards")


@op("ui", name="type", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Type text character by character, paced when recording",
    examples=[{"target": {"by": "placeholder", "args": {"text": "Ara"}}, "value": "Kadıköy", "pace": "demo"}])
class TypeText(Act):
    target: Target = Field(description="Input to type into")
    value: RStr = Field(description="Text typed character by character")
    pace: str | None = Field(
        default=None, description="Pace preset for the keystrokes while recording; the recording's pace when unset")
    submit: bool = Field(default=False, description="Press Enter or the keyboard's submit key afterwards")


@op("ui", name="clear", platforms=UI, resources=LEASE, location="worker", pinnable=True, description="Clear an input",
    examples=[{"target": {"by": "placeholder", "args": {"text": "Ara"}}}])
class Clear(Act):
    target: Target = Field(description="Input to clear")


@op("ui", name="key", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Press a key, optionally focused on a target",
    examples=[{"key": "Enter"}])
class Key(Act):
    key: str = Field(min_length=1, description="Key name such as Enter, Escape or Control+A")
    target: Target | None = Field(default=None, description="Element to focus first; the focused element when unset")


@op("ui", name="select", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Pick options of a select",
    examples=[{"target": {"by": "test_id", "args": {"id": "currency"}}, "value": "TRY"}])
class Select(Act):
    target: Target = Field(description="Select element or picker")
    value: RStr | tuple[RStr, ...] = Field(description="Option label, or several labels for a multiple select")


@op("ui", name="check", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Set a checkbox or switch",
    examples=[{"target": {"by": "test_id", "args": {"id": "e-archive"}}, "on": True}])
class Check(Act):
    target: Target = Field(description="Checkbox or switch")
    on: bool = Field(default=True, description="State to set: checked when true, unchecked when false")


@op("ui", name="choose", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Open a dropdown or menu trigger and pick an option",
    examples=[{"trigger": {"by": "test_id", "args": {"id": "branch-picker"}}, "option": "Kadıköy"}])
class Choose(Act):
    trigger: Target = Field(description="Element that opens the dropdown or menu")
    option: RStr = Field(description="Visible text of the option to pick")


@op("ui", name="scroll", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Scroll a container to an edge, to a target or by an offset",
    examples=[{"target": {"by": "test_id", "args": {"id": "invoice-list"}}, "to": "bottom"}])
class Scroll(Act):
    target: Target | None = Field(
        default=None, description="Scroll container; the page or the main scroll view when unset")
    to: ScrollEdge | Target | None = Field(
        default=None, description="Edge to scroll to, or an element to bring into view")
    by: tuple[int, int] | None = Field(default=None, description="Offset to scroll by as (x, y) in pixels")


@op("ui", name="swipe", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Swipe in a direction",
    examples=[{"direction": "left", "target": {"by": "test_id", "args": {"id": "id-pages"}}}])
class Swipe(Act):
    direction: SwipeDirection = Field(description="Direction the finger moves")
    target: Target | None = Field(default=None, description="Element to swipe on; the screen when unset")
    distance: float | None = Field(
        default=None, gt=0.0, le=1.0, description="Swipe length as a fraction of the element or screen size")


@op("ui", name="drag", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Drag a target onto another",
    examples=[
        {
            "source": {"by": "test_id", "args": {"id": "column-amount"}},
            "to": {"by": "test_id", "args": {"id": "column-date"}},
        },
    ])
class Drag(Act):
    source: Target = Field(description="Element to pick up")
    to: Target = Field(description="Element to drop onto")


@op("ui", name="text", out="str", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Visible text of a target or of the page",
    examples=[{"target": {"by": "role", "args": {"role": "heading"}}}])
class ReadText(Args):
    target: Target | None = Field(default=None, description="Element to read; the whole page or screen when unset")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="value", out="str", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Value of an input",
    examples=[{"target": {"by": "test_id", "args": {"id": "amount"}}}])
class ReadValue(Args):
    target: Target = Field(description="Input to read")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="attr", out="json", platforms=WEB, resources=LEASE, location="worker", idempotent=True,
    description="An attribute of a target, null when absent",
    examples=[{"target": {"by": "role", "args": {"role": "link", "name": "Faturalar"}}, "name": "href"}])
class ReadAttr(Args):
    target: Target = Field(description="Element to read")
    name: str = Field(min_length=1, description="Attribute name such as href or aria-expanded")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="count", out="int", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Number of visible matches of a target",
    examples=[{"target": {"by": "test_id", "args": {"id": "invoice-row"}}}])
class Count(Args):
    target: Target = Field(description="Target whose visible matches are counted")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="exists", out="bool", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Whether a target matches anything, visible or not",
    examples=[{"target": {"by": "test_id", "args": {"id": "empty-state"}}}])
class Exists(Args):
    target: Target = Field(description="Target to look for")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="visible", out="bool", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Whether a target is visible",
    examples=[{"target": {"by": "role", "args": {"role": "dialog"}}}])
class Visible(Args):
    target: Target = Field(description="Target to check")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="tree", out="str", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Accessibility or UI tree as text",
    examples=[{"max_lines": 200}])
class Tree(Args):
    target: Target | None = Field(default=None, description="Subtree root; the whole screen when unset")
    max_lines: int | None = Field(default=None, ge=1, description="Cut the text after this many lines")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="url", out="str", platforms=WEB, resources=LEASE, location="worker", idempotent=True,
    description="Current URL",
    examples=[{}])
class Url(Args):
    pass


@op("ui", name="route", out="json", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Current route: name and params on React Native, path and query on web",
    examples=[{}])
class Route(Args):
    pass


@op("ui", name="state", out="json", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="App state at a dotted path (React Native store, or the web app's state hook)",
    examples=[{"path": "invoice.draft"}])
class State(Args):
    path: str | None = Field(default=None, description="Dotted path into the state such as cart.items; the whole state when unset")


@op("ui", name="layout", out="json", platforms=UI, resources=LEASE, location="worker", idempotent=True,
    description="Box of a target in viewport pixels",
    examples=[{"target": {"by": "test_id", "args": {"id": "total"}}}])
class Layout(Args):
    target: Target = Field(description="Element to measure")
    timeout: Duration | None = Field(default=None, description='How long to wait for the target to match; the session default when unset')


@op("ui", name="wait_for", platforms=UI, resources=LEASE, location="worker",
    description="Wait until a target reaches a state",
    examples=[{"target": {"by": "test_id", "args": {"id": "invoice-row"}}, "state": "visible", "timeout": "5s"}])
class WaitFor(Args):
    target: Target = Field(description="Target to wait for")
    state: WaitState = Field(default="visible", description="State the target must reach")
    timeout: Duration | None = Field(default=None, description="How long to wait; the session default when unset")


@op("ui", name="wait_gone", platforms=UI, resources=LEASE, location="worker", description="Wait until a target is gone",
    examples=[{"target": {"by": "test_id", "args": {"id": "spinner"}}}])
class WaitGone(Args):
    target: Target = Field(description="Target that must disappear")
    timeout: Duration | None = Field(default=None, description="How long to wait; the session default when unset")


@op("ui", name="wait_url", out="str", platforms=WEB, resources=LEASE, location="worker",
    description="Wait until the URL contains a pattern",
    examples=[{"pattern": "/tenant/invoice/*"}])
class WaitUrl(Args):
    pattern: RStr = Field(description="Text the URL must contain")
    timeout: Duration | None = Field(default=None, description="How long to wait; the session default when unset")


@op("ui", name="settle", platforms=UI, resources=LEASE, location="worker",
    description="Wait until network, DOM or React Native work is idle; strict settles fail on timeout",
    examples=[{"strict": True}])
class Settle(Args):
    strict: bool = Field(default=True, description="Fail the run when the app does not settle in time")
    timeout: Duration | None = Field(default=None, description="How long to wait; the session default when unset")


__all__ = [
    "LEASE",
    "MOBILE",
    "UI",
    "WEB",
    "Act",
    "Back",
    "Check",
    "Choose",
    "Clear",
    "Count",
    "Deeplink",
    "DoubleTap",
    "Drag",
    "Exists",
    "Fill",
    "Hover",
    "Key",
    "Layout",
    "LongPress",
    "Nav",
    "OpenUrl",
    "ReadAttr",
    "ReadText",
    "ReadValue",
    "Reload",
    "Route",
    "Scroll",
    "ScrollEdge",
    "Select",
    "Settle",
    "State",
    "Swipe",
    "SwipeDirection",
    "Tap",
    "Tree",
    "TypeText",
    "Url",
    "Visible",
    "WaitFor",
    "WaitGone",
    "WaitState",
    "WaitUrl",
]
