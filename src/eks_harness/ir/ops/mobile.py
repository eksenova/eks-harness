from __future__ import annotations

from pydantic import Field

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.ops.ui import LEASE, MOBILE
from eks_harness.ir.refs import RJson, RStr
from eks_harness.ir.registry import Args, op
from eks_harness.ir.target import Target


@op("mobile", name="dispatch", out="json", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Dispatch an action to the app store",
    examples=[{"action": {"type": "invoice/reset"}}])
class Dispatch(Args):
    action: RJson = Field(description="Action object passed to the store's dispatch, such as {'type': 'cart/clear'}")


@op("mobile", name="patch", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Override props, style or text of a rendered component until unpatched",
    examples=[{"target": {"by": "test_id", "args": {"id": "clock"}}, "text": "09:41", "key": "clock"}])
class Patch(Args):
    target: Target = Field(description="Rendered component to patch")
    props: dict[str, RJson] | None = Field(default=None, description="Props merged over the component's own props")
    style: dict[str, RJson] | None = Field(default=None, description="Style merged over the component's style")
    text: RStr | None = Field(default=None, description="Text that replaces the component's text content")
    key: str | None = Field(default=None, description="Name of the patch, used by mobile.unpatch")


@op("mobile", name="unpatch", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Remove a patch by key",
    examples=[{"key": "clock"}])
class Unpatch(Args):
    key: str = Field(min_length=1, description="Name the patch was given")


@op("mobile", name="mount", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Mount a registered component into the app, optionally next to a target",
    examples=[{"component": "DebugBanner", "props": {"text": "staging"}, "key": "banner"}])
class Mount(Args):
    component: str = Field(min_length=1, description="Name of a component registered with the harness bridge")
    props: dict[str, RJson] | None = Field(default=None, description="Props passed to the component")
    target: Target | None = Field(default=None, description="Element to mount next to; the app root when unset")
    key: str | None = Field(default=None, description="Name of the mount, used by mobile.unmount")


@op("mobile", name="unmount", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Remove a mounted component by key",
    examples=[{"key": "banner"}])
class Unmount(Args):
    key: str = Field(min_length=1, description="Name the mount was given")


@op("mobile", name="invoke", out="json", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Call a handler prop of a component with arguments",
    examples=[{"target": {"by": "test_id", "args": {"id": "amount"}}, "prop": "onChangeText", "args": ["250"]}])
class Invoke(Args):
    target: Target = Field(description="Component whose handler is called")
    prop: str = Field(min_length=1, description="Handler prop name such as onPress or onChangeText")
    args: tuple[RJson, ...] = Field(default=(), description="Arguments passed to the handler")


@op("mobile", name="toggle", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Toggle a switch",
    examples=[{"target": {"by": "test_id", "args": {"id": "e-archive"}}, "value": True}])
class Toggle(Args):
    target: Target = Field(description="Switch to toggle")
    value: bool | None = Field(default=None, description="State to set; flips the current state when unset")
    timeout: Duration | None = Field(default=None, description="How long to wait for the switch; the session default when unset")


@op("mobile", name="submit", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Submit an input",
    examples=[{"target": {"by": "test_id", "args": {"id": "amount"}}}])
class Submit(Args):
    target: Target = Field(description="Input whose onSubmitEditing is triggered")
    timeout: Duration | None = Field(default=None, description="How long to wait for the input; the session default when unset")


@op("mobile", name="idle", platforms=MOBILE, resources=LEASE, location="worker",
    description="Wait until the JS thread is idle",
    examples=[{"timeout": "5s"}])
class Idle(Args):
    timeout: Duration | None = Field(default=None, description="How long to wait; the session default when unset")


@op("mobile", name="route_names", out="json", platforms=MOBILE, resources=LEASE, location="worker", idempotent=True,
    description="Route names known to the navigator",
    examples=[{}])
class RouteNames(Args):
    pass


@op("mobile", name="logout", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Log the app out",
    examples=[{}])
class Logout(Args):
    pass


@op("mobile", name="close_dev_menu", platforms=MOBILE, resources=LEASE, location="worker",
    description="Close the React Native dev menu",
    examples=[{}])
class CloseDevMenu(Args):
    pass


@op("mobile", name="pointer", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Move the visible pointer to a target, or hide it",
    examples=[{"target": {"by": "test_id", "args": {"id": "save"}}, "duration": "300ms"}])
class Pointer(Args):
    target: Target | None = Field(default=None, description="Element to move the pointer to; hides the pointer when unset")
    duration: Duration | None = Field(default=None, description="Length of the pointer movement; the pace preset's move time when unset")


__all__ = [
    "CloseDevMenu",
    "Dispatch",
    "Idle",
    "Invoke",
    "Logout",
    "Mount",
    "Patch",
    "Pointer",
    "RouteNames",
    "Submit",
    "Toggle",
    "Unmount",
    "Unpatch",
]
