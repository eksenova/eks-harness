from typing import Any

import pytest

import eks
from eks import by, t
from eks.ref import Ref, type_of
from eks_harness.ir.hashing import resource_id
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.target import TargetError

LEASE = resource_id("lease.web", {"app": "web-app", "session": "main"})
CREATE = {"by": "test_id", "args": {"id": "create"}}
ROW = {"by": "test_id", "args": {"id": "row"}}
SAVE = {"by": "text", "args": {"text": "Kaydet", "exact": True}}
ACT = {"settle": True}


def ops(plan: Plan) -> list[tuple[str, dict[str, Any]]]:
    return [(node.op, dict(node.args)) for node in plan.nodes]


def only(plan: Plan) -> Node:
    (node,) = plan.nodes
    return node


def assert_deterministic(fn: Any, **params: Any) -> Plan:
    first = eks.plan(fn, **params)
    second = eks.plan(fn, **params)
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    return first


def test_tap_records_the_target_and_defaults() -> None:
    @eks.flow(app="web-app", platform="web")
    def tapping(s: eks.Session) -> None:
        s.ui.tap("#create")

    node = only(assert_deterministic(tapping))
    assert node.op == "ui.tap"
    assert node.args == {"target": CREATE, "force": False, "settle": True}
    assert node.out == "none"
    assert node.uses == [LEASE]
    assert node.pin is None


def test_navigation_ops() -> None:
    @eks.flow(app="web-app", platform="web")
    def navigating(s: eks.Session) -> None:
        s.ui.nav("/tenant/settings/branches")
        s.ui.nav("Invoice", {"id": 7, "tab": "lines"}, timeout=t("5s"))
        s.ui.back()
        s.ui.back(timeout=t("2s"))
        s.ui.reload()
        s.ui.open_url("https://eksenova.com/x")
        s.ui.deeplink("eksengold://invoice/7", timeout=t("3s"))

    plan = assert_deterministic(navigating)
    assert ops(plan) == [
        ("ui.nav", {"route": "/tenant/settings/branches"}),
        ("ui.nav", {"route": "Invoice", "params": {"id": 7, "tab": "lines"}, "timeout": "5s"}),
        ("ui.back", {}),
        ("ui.back", {"timeout": "2s"}),
        ("ui.reload", {}),
        ("ui.open_url", {"url": "https://eksenova.com/x"}),
        ("ui.deeplink", {"url": "eksengold://invoice/7", "timeout": "3s"}),
    ]
    assert all(node.uses == [LEASE] for node in plan.nodes)


def test_nav_interpolates_refs_into_fmt() -> None:
    @eks.flow(app="web-app", platform="web")
    def interpolating(s: eks.Session) -> None:
        branches = s.ui.state("branches")
        ref = branches.items.find(name="Kadıköy").id
        s.ui.nav(f"/tenant/invoice/list?branchId={ref}")

    state, nav = assert_deterministic(interpolating).nodes
    assert state.op == "ui.state"
    assert nav.args == {
        "route": {"$fmt": [
            "/tenant/invoice/list?branchId=",
            {"$ref": state.id, "path": ["items", {"find": {"name": "Kadıköy"}}, "id"]},
        ]},
    }
    assert nav.after == [state.id]


def test_nav_accepts_a_ref_route_and_ref_params() -> None:
    @eks.flow(app="web-app", platform="web")
    def ref_route(s: eks.Session) -> None:
        route = s.ui.state("next.route")
        s.ui.nav(route.name, {"id": route.params.id})

    state, nav = eks.plan(ref_route).nodes
    assert nav.args == {
        "route": {"$ref": state.id, "path": ["name"]},
        "params": {"id": {"$ref": state.id, "path": ["params", "id"]}},
    }


def test_every_input_op() -> None:
    @eks.flow(app="web-app", platform="web")
    def inputs(s: eks.Session) -> None:
        s.ui.tap(by.text("Kaydet"), force=True, real=True, timeout=t("4s"), settle=False)
        s.ui.double_tap("#create")
        s.ui.long_press("#create", hold=t("800ms"))
        s.ui.hover(by.role("button", name="Menü"), real=True)
        s.ui.fill("#email", "a@b.c", submit=True)
        s.ui.type("#search", "Kadıköy", pace="demo")
        s.ui.clear("#search")
        s.ui.key("Enter")
        s.ui.key("Escape", "#dialog", timeout=t("1s"))
        s.ui.check("#agree")
        s.ui.check("#agree", False)
        s.ui.choose("#branch", "Kadıköy")
        s.ui.swipe("up")
        s.ui.swipe("left", target="#carousel", distance=0.5)
        s.ui.drag("#card", "#done")

    plan = assert_deterministic(inputs)
    assert ops(plan) == [
        ("ui.tap", {"target": SAVE, "force": True, "real": True, "timeout": "4s", "settle": False}),
        ("ui.double_tap", {"target": CREATE, **ACT}),
        ("ui.long_press", {"target": CREATE, "hold": "0.8s", **ACT}),
        ("ui.hover", {"target": {"by": "role", "args": {"role": "button", "name": "Menü", "exact": False}},
                      "real": True, **ACT}),
        ("ui.fill", {"target": {"by": "test_id", "args": {"id": "email"}}, "value": "a@b.c", "submit": True, **ACT}),
        ("ui.type", {"target": {"by": "test_id", "args": {"id": "search"}}, "value": "Kadıköy", "pace": "demo",
                     "submit": False, **ACT}),
        ("ui.clear", {"target": {"by": "test_id", "args": {"id": "search"}}, **ACT}),
        ("ui.key", {"key": "Enter", **ACT}),
        ("ui.key", {"key": "Escape", "target": {"by": "test_id", "args": {"id": "dialog"}}, "timeout": "1s", **ACT}),
        ("ui.check", {"target": {"by": "test_id", "args": {"id": "agree"}}, "on": True, **ACT}),
        ("ui.check", {"target": {"by": "test_id", "args": {"id": "agree"}}, "on": False, **ACT}),
        ("ui.choose", {"trigger": {"by": "test_id", "args": {"id": "branch"}}, "option": "Kadıköy", **ACT}),
        ("ui.swipe", {"direction": "up", **ACT}),
        ("ui.swipe", {"direction": "left", "target": {"by": "test_id", "args": {"id": "carousel"}}, "distance": 0.5,
                      **ACT}),
        ("ui.drag", {"source": {"by": "test_id", "args": {"id": "card"}}, "to": {"by": "test_id", "args": {"id": "done"}},
                     **ACT}),
    ]
    assert all(node.uses == [LEASE] for node in plan.nodes)
    assert [node.after for node in plan.nodes[1:]] == [[node.id] for node in plan.nodes[:-1]]


def test_scroll_to_an_edge_or_a_target() -> None:
    @eks.flow(app="web-app", platform="web")
    def scrolling(s: eks.Session) -> None:
        s.ui.scroll(to="bottom")
        s.ui.scroll("#list", to="top")
        s.ui.scroll("#list", to="start")
        s.ui.scroll(to="end")
        s.ui.scroll(to="#row")
        s.ui.scroll("#list", to=by.text("Kaydet"))
        s.ui.scroll("#list", by=(0, 300))

    plan = assert_deterministic(scrolling)
    lst = {"by": "test_id", "args": {"id": "list"}}
    assert [args for _op, args in ops(plan)] == [
        {"to": "bottom", **ACT},
        {"target": lst, "to": "top", **ACT},
        {"target": lst, "to": "start", **ACT},
        {"to": "end", **ACT},
        {"to": ROW, **ACT},
        {"target": lst, "to": SAVE, **ACT},
        {"target": lst, "by": [0, 300], **ACT},
    ]


def test_scroll_to_a_bare_word_is_a_text_target() -> None:
    @eks.flow(app="web-app", platform="web")
    def scrolling(s: eks.Session) -> None:
        s.ui.scroll(to="Toplam")

    assert only(eks.plan(scrolling)).args["to"] == {"by": "text", "args": {"text": "Toplam", "exact": True}}


def test_select_takes_one_value_or_a_sequence() -> None:
    @eks.flow(app="web-app", platform="web")
    def selecting(s: eks.Session) -> None:
        current = s.ui.value("#currency")
        s.ui.select("#currency", "TRY")
        s.ui.select("#tags", ["altın", "gümüş"])
        s.ui.select("#tags", ("altın", current))
        s.ui.select("#currency", current)

    value, one, many, mixed, from_ref = assert_deterministic(selecting).nodes
    select = {"by": "test_id", "args": {"id": "currency"}}
    tags = {"by": "test_id", "args": {"id": "tags"}}
    assert one.args == {"target": select, "value": "TRY", **ACT}
    assert many.args == {"target": tags, "value": ["altın", "gümüş"], **ACT}
    assert mixed.args == {"target": tags, "value": ["altın", {"$ref": value.id, "path": []}], **ACT}
    assert from_ref.args == {"target": select, "value": {"$ref": value.id, "path": []}, **ACT}


def test_select_rejects_other_values() -> None:
    @eks.flow(app="web-app", platform="web")
    def bad(s: eks.Session) -> None:
        s.ui.select("#tags", 3)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="s.ui.select value"):
        eks.plan(bad)

    @eks.flow(app="web-app", platform="web")
    def bad_item(s: eks.Session) -> None:
        s.ui.select("#tags", ["a", 3])  # type: ignore[list-item]

    with pytest.raises(TypeError, match=r"s.ui.select value\[1\]"):
        eks.plan(bad_item)


def test_reads_return_typed_refs() -> None:
    seen: dict[str, Ref[Any]] = {}

    @eks.flow(app="web-app", platform="web")
    def reading(s: eks.Session) -> None:
        seen["text"] = s.ui.text("#title")
        seen["page_text"] = s.ui.text()
        seen["value"] = s.ui.value("#email")
        seen["attr"] = s.ui.attr("#link", "href")
        seen["count"] = s.ui.count("#row")
        seen["exists"] = s.ui.exists("#empty-state")
        seen["visible"] = s.ui.visible("#toast")
        seen["tree"] = s.ui.tree("#main", max_lines=40)
        seen["page_tree"] = s.ui.tree()
        seen["url"] = s.ui.url()
        seen["route"] = s.ui.route()
        seen["state"] = s.ui.state("cart.items")
        seen["whole_state"] = s.ui.state()
        seen["layout"] = s.ui.layout("#card")

    plan = assert_deterministic(reading)
    assert {name: type_of(ref) for name, ref in seen.items()} == {
        "text": "str", "page_text": "str", "value": "str", "attr": "json", "count": "int", "exists": "bool",
        "visible": "bool", "tree": "str", "page_tree": "str", "url": "str", "route": "json", "state": "json",
        "whole_state": "json", "layout": "json",
    }
    assert [node.out for node in plan.nodes] == [type_of(ref) for ref in seen.values()]
    assert ops(plan) == [
        ("ui.text", {"target": {"by": "test_id", "args": {"id": "title"}}}),
        ("ui.text", {}),
        ("ui.value", {"target": {"by": "test_id", "args": {"id": "email"}}}),
        ("ui.attr", {"target": {"by": "test_id", "args": {"id": "link"}}, "name": "href"}),
        ("ui.count", {"target": ROW}),
        ("ui.exists", {"target": {"by": "test_id", "args": {"id": "empty-state"}}}),
        ("ui.visible", {"target": {"by": "test_id", "args": {"id": "toast"}}}),
        ("ui.tree", {"target": {"by": "test_id", "args": {"id": "main"}}, "max_lines": 40}),
        ("ui.tree", {}),
        ("ui.url", {}),
        ("ui.route", {}),
        ("ui.state", {"path": "cart.items"}),
        ("ui.state", {}),
        ("ui.layout", {"target": {"by": "test_id", "args": {"id": "card"}}}),
    ]
    assert all(node.uses == [LEASE] for node in plan.nodes)


def test_waits() -> None:
    @eks.flow(app="web-app", platform="web")
    def waiting(s: eks.Session) -> Ref[str]:
        s.ui.wait_for("#toast")
        s.ui.wait_for("#dialog", state="hidden", timeout=t("10s"))
        s.ui.wait_gone("#spinner", timeout=t("8s"))
        s.ui.settle()
        s.ui.settle(strict=False, timeout=t("2s"))
        return s.ui.wait_url("/tenant/invoice", timeout=t("5s"))

    plan = assert_deterministic(waiting)
    assert ops(plan) == [
        ("ui.wait_for", {"target": {"by": "test_id", "args": {"id": "toast"}}, "state": "visible"}),
        ("ui.wait_for", {"target": {"by": "test_id", "args": {"id": "dialog"}}, "state": "hidden", "timeout": "10s"}),
        ("ui.wait_gone", {"target": {"by": "test_id", "args": {"id": "spinner"}}, "timeout": "8s"}),
        ("ui.settle", {"strict": True}),
        ("ui.settle", {"strict": False, "timeout": "2s"}),
        ("ui.wait_url", {"pattern": "/tenant/invoice", "timeout": "5s"}),
    ]
    assert plan.nodes[-1].out == "str"
    assert plan.outputs == {"$ref": plan.nodes[-1].id, "path": []}


def test_read_refs_feed_later_ops() -> None:
    @eks.flow(app="web-app", platform="web")
    def chained(s: eks.Session) -> None:
        name = s.ui.text("#customer-name")
        s.ui.fill("#search", name)
        s.ui.choose("#branch", f"{name} şubesi")

    text, fill, choose = eks.plan(chained).nodes
    assert fill.args["value"] == {"$ref": text.id, "path": []}
    assert choose.args["option"] == {"$fmt": [{"$ref": text.id, "path": []}, " şubesi"]}
    assert fill.after == [text.id]
    assert choose.after == [fill.id, text.id]


def test_numbers_are_not_times() -> None:
    @eks.flow(app="web-app", platform="web")
    def numeric_hold(s: eks.Session) -> None:
        s.ui.long_press("#create", hold=5)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match=r"s.ui.long_press hold expects a Time.*t\('1.5s'\)"):
        eks.plan(numeric_hold)

    @eks.flow(app="web-app", platform="web")
    def string_timeout(s: eks.Session) -> None:
        s.ui.tap("#create", timeout="2s")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match=r"s.ui.tap timeout expects a Time.*t\('2s'\)"):
        eks.plan(string_timeout)


def test_targets_must_be_targets_or_strings() -> None:
    @eks.flow(app="web-app", platform="web")
    def bad_target(s: eks.Session) -> None:
        s.ui.tap(42)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="s.ui.tap target must be a Target"):
        eks.plan(bad_target)


def test_bad_target_shorthand_raises_a_target_error() -> None:
    @eks.flow(app="web-app", platform="web")
    def bad_shorthand(s: eks.Session) -> None:
        s.ui.tap("role=")

    with pytest.raises((TargetError, ValueError)):
        eks.plan(bad_shorthand)


def test_text_parameters_reject_non_strings() -> None:
    @eks.flow(app="web-app", platform="web")
    def numeric_value(s: eks.Session) -> None:
        s.ui.fill("#amount", 12)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="s.ui.fill value must be a string or a Ref"):
        eks.plan(numeric_value)


def test_type_pace_must_be_a_preset_name() -> None:
    @eks.flow(app="web-app", platform="web")
    def paced(s: eks.Session) -> None:
        s.ui.type("#search", "x", pace=3)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="pace preset"):
        eks.plan(paced)


def test_at_pins_the_node() -> None:
    @eks.flow(app="web-app", platform="web")
    def pinned(s: eks.Session) -> None:
        s.ui.tap("#create", at=t("bar:1"))
        s.ui.nav("/x", at=t("1.5s"))

    tap, nav = assert_deterministic(pinned).nodes
    assert tap.pin == t("bar:1")
    assert nav.pin == t("1.5s")
    dumped = assert_deterministic(pinned).model_dump(mode="json", by_alias=True, exclude_none=True)
    assert [node["pin"] for node in dumped["nodes"]] == ["bar:1", "1.5s"]


def test_at_must_be_a_time() -> None:
    @eks.flow(app="web-app", platform="web")
    def pinned(s: eks.Session) -> None:
        s.ui.tap("#create", at=2)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="ui.tap at expects a Time"):
        eks.plan(pinned)


def test_flow_persona_leads_with_session_as() -> None:
    @eks.flow(app="web-app", platform="web", as_="harman-admin")
    def persona(s: eks.Session) -> None:
        s.ui.tap("#create")

    as_node, tap = assert_deterministic(persona).nodes
    assert as_node.op == "session.as"
    assert tap.after == [as_node.id]
    assert tap.uses == [LEASE]


def test_ui_ops_carry_the_platform_lease() -> None:
    @eks.flow(app="mobile-app", platform="android")
    def android(s: eks.Session) -> None:
        s.ui.tap("#create")
        s.ui.state("cart")

    plan = eks.plan(android)
    lease = resource_id("lease.android", {"app": "mobile-app", "session": "main"})
    assert [node.uses for node in plan.nodes] == [[lease], [lease]]
    assert lease in {resource.id for resource in plan.resources}


def test_ids_differ_per_call_site_and_occurrence() -> None:
    @eks.flow(app="web-app", platform="web")
    def repeated(s: eks.Session) -> None:
        for _ in range(2):
            s.ui.tap("#create")
        s.ui.tap("#create")

    plan = eks.plan(repeated)
    ids = [node.id for node in plan.nodes]
    assert len(set(ids)) == 3
