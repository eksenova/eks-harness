from typing import Any

import pytest

import eks
from eks import by, t
from eks.ref import Ref, type_of
from eks.tracing import TraceError
from eks_harness.ir.hashing import resource_id
from eks_harness.ir.ops.web import ClearLogs
from eks_harness.ir.plan import Plan

WEB_LEASE = resource_id("lease.web", {"app": "web-app", "session": "main"})
IOS_LEASE = resource_id("lease.ios", {"app": "mobile-app", "session": "main"})


def ops(plan: Plan) -> list[tuple[str, dict[str, Any]]]:
    return [(node.op, dict(node.args)) for node in plan.nodes]


def twice(fn: Any) -> Plan:
    first = eks.plan(fn)
    second = eks.plan(fn)
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    return first


def test_web_ops() -> None:
    refs: dict[str, Ref[Any]] = {}

    @eks.flow(app="web-app", platform="web")
    def web(s: eks.Session) -> None:
        s.web.wheel()
        s.web.wheel("#list", dx=10, dy=-200, real=True, at=t("2s"))
        s.web.viewport("phone")
        s.web.viewport("desktop", at=t("bar:2"))
        refs["console"] = s.web.logs()
        refs["network"] = s.web.logs("network", limit=20)
        s.web.clear_logs()

    plan = twice(web)
    assert ops(plan) == [
        ("web.wheel", {"dx": 0, "dy": 400}),
        ("web.wheel", {"target": {"by": "test_id", "args": {"id": "list"}}, "dx": 10, "dy": -200, "real": True}),
        ("web.viewport", {"name": "phone"}),
        ("web.viewport", {"name": "desktop"}),
        ("web.logs", {"kind": "console", "limit": 100}),
        ("web.logs", {"kind": "network", "limit": 20}),
        ("web.clear_logs", {}),
    ]
    assert [node.pin for node in plan.nodes] == [None, t("2s"), None, t("bar:2"), None, None, None]
    assert all(node.uses == [WEB_LEASE] for node in plan.nodes)
    assert {name: type_of(ref) for name, ref in refs.items()} == {"console": "json", "network": "json"}


def test_wheel_pin_sits_between_unpinned_nodes() -> None:
    @eks.flow(app="web-app", platform="web")
    def pinned(s: eks.Session) -> None:
        s.ui.settle()
        s.web.wheel(at=t("1s"))
        s.ui.count("#row")

    plan = eks.plan(pinned)
    assert plan.nodes[1].pin == t("1s")


def test_unpinnable_ops_reject_at_through_the_session() -> None:
    @eks.flow(app="web-app", platform="web")
    def pinned(s: eks.Session) -> None:
        s.op("web.logs", kind="errors")
        s.call(ClearLogs(), at=t("1s"))

    with pytest.raises(TraceError, match="web.clear_logs cannot be pinned"):
        eks.plan(pinned)


def test_mobile_ops_on_ios() -> None:
    refs: dict[str, Ref[Any]] = {}

    @eks.flow(app="mobile-app", platform="ios")
    def ios(s: eks.Session) -> None:
        refs["dispatch"] = s.mobile.dispatch({"type": "cart/clear"})
        refs["dispatch_at"] = s.mobile.dispatch({"type": "cart/add", "payload": {"sku": "22A"}}, at=t("bar:1"))
        s.mobile.patch("#balance", props={"amount": 1250}, style={"color": "#d4af37"}, text="1.250 TL", key="bal")
        s.mobile.patch(by.text("Bakiye"), text="Toplam")
        s.mobile.unpatch("bal")
        s.mobile.mount("PromoBanner", props={"title": "Altın"}, target="#home-header", key="promo")
        s.mobile.mount("Confetti")
        s.mobile.unmount("promo")
        refs["invoke"] = s.mobile.invoke("#list", "onEndReached")
        refs["invoke_args"] = s.mobile.invoke("#list", "onScroll", {"y": 120}, 3, "fast", at=t("1s"))
        s.mobile.toggle("#notifications")
        s.mobile.toggle("#notifications", True, timeout=t("2s"))
        s.mobile.submit("#search", timeout=t("1s"))
        s.mobile.idle()
        s.mobile.idle(timeout=t("5s"))
        refs["routes"] = s.mobile.route_names()
        s.mobile.logout()
        s.mobile.close_dev_menu()
        s.mobile.pointer("#save", duration=t("300ms"))
        s.mobile.pointer()
        refs["state"] = s.mobile.state("cart.items")
        refs["whole_state"] = s.mobile.state()

    plan = twice(ios)
    balance = {"by": "test_id", "args": {"id": "balance"}}
    lst = {"by": "test_id", "args": {"id": "list"}}
    toggle = {"by": "test_id", "args": {"id": "notifications"}}
    assert ops(plan) == [
        ("mobile.dispatch", {"action": {"type": "cart/clear"}}),
        ("mobile.dispatch", {"action": {"type": "cart/add", "payload": {"sku": "22A"}}}),
        ("mobile.patch", {"target": balance, "props": {"amount": 1250}, "style": {"color": "#d4af37"},
                          "text": "1.250 TL", "key": "bal"}),
        ("mobile.patch", {"target": {"by": "text", "args": {"text": "Bakiye", "exact": True}}, "text": "Toplam"}),
        ("mobile.unpatch", {"key": "bal"}),
        ("mobile.mount", {"component": "PromoBanner", "props": {"title": "Altın"},
                          "target": {"by": "test_id", "args": {"id": "home-header"}}, "key": "promo"}),
        ("mobile.mount", {"component": "Confetti"}),
        ("mobile.unmount", {"key": "promo"}),
        ("mobile.invoke", {"target": lst, "prop": "onEndReached", "args": []}),
        ("mobile.invoke", {"target": lst, "prop": "onScroll", "args": [{"y": 120}, 3, "fast"]}),
        ("mobile.toggle", {"target": toggle}),
        ("mobile.toggle", {"target": toggle, "value": True, "timeout": "2s"}),
        ("mobile.submit", {"target": {"by": "test_id", "args": {"id": "search"}}, "timeout": "1s"}),
        ("mobile.idle", {}),
        ("mobile.idle", {"timeout": "5s"}),
        ("mobile.route_names", {}),
        ("mobile.logout", {}),
        ("mobile.close_dev_menu", {}),
        ("mobile.pointer", {"target": {"by": "test_id", "args": {"id": "save"}}, "duration": "0.3s"}),
        ("mobile.pointer", {}),
        ("ui.state", {"path": "cart.items"}),
        ("ui.state", {}),
    ]
    assert all(node.uses == [IOS_LEASE] for node in plan.nodes)
    assert plan.nodes[1].pin == t("bar:1")
    assert plan.nodes[9].pin == t("1s")
    assert {name: type_of(ref) for name, ref in refs.items()} == {
        "dispatch": "json", "dispatch_at": "json", "invoke": "json", "invoke_args": "json", "routes": "json",
        "state": "json", "whole_state": "json",
    }
    assert plan.session is not None
    assert plan.session.platform == "ios"


def test_mobile_state_is_the_same_op_as_ui_state() -> None:
    @eks.flow(app="mobile-app", platform="ios")
    def states(s: eks.Session) -> None:
        s.mobile.state("cart.items")
        s.ui.state("cart.items")

    mobile, ui = eks.plan(states).nodes
    assert (mobile.op, mobile.args, mobile.out, mobile.uses) == (ui.op, ui.args, ui.out, ui.uses)


def test_mobile_args_take_refs() -> None:
    @eks.flow(app="mobile-app", platform="ios")
    def refs(s: eks.Session) -> None:
        cart = s.mobile.state("cart")
        s.mobile.dispatch({"type": "cart/remove", "id": cart.items.first.id})
        s.mobile.dispatch(cart.pending)
        s.mobile.patch("#total", text=f"{cart.total} TL", props={"count": cart.items.len()})
        s.mobile.invoke("#list", "onSelect", cart.items.last)

    cart, dispatch, dispatch_ref, patch, invoke = eks.plan(refs).nodes
    assert dispatch.args == {"action": {"type": "cart/remove", "id": {"$ref": cart.id, "path": ["items", 0, "id"]}}}
    assert dispatch_ref.args == {"action": {"$ref": cart.id, "path": ["pending"]}}
    assert patch.args["text"] == {"$fmt": [{"$ref": cart.id, "path": ["total"]}, " TL"]}
    assert patch.args["props"] == {"count": {"$ref": cart.id, "path": ["items", {"len": True}]}}
    assert invoke.args["args"] == [{"$ref": cart.id, "path": ["items", -1]}]


def test_mobile_input_errors() -> None:
    @eks.flow(app="mobile-app", platform="ios")
    def bad_action(s: eks.Session) -> None:
        s.mobile.dispatch("cart/clear")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="dispatch action must be a mapping"):
        eks.plan(bad_action)

    @eks.flow(app="mobile-app", platform="ios")
    def bad_props(s: eks.Session) -> None:
        s.mobile.patch("#x", props=["a"])  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="s.mobile.patch props must be a mapping"):
        eks.plan(bad_props)

    @eks.flow(app="mobile-app", platform="ios")
    def bad_duration(s: eks.Session) -> None:
        s.mobile.pointer("#x", duration=1)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="s.mobile.pointer duration expects a Time"):
        eks.plan(bad_duration)


def test_android_mobile_ops_use_the_android_lease() -> None:
    @eks.flow(app="mobile-app", platform="android")
    def android(s: eks.Session) -> None:
        s.mobile.close_dev_menu()

    (node,) = eks.plan(android).nodes
    assert node.uses == [resource_id("lease.android", {"app": "mobile-app", "session": "main"})]
