import json
from typing import Any, cast

import eks
from eks import by
from eks.ref import Ref, type_of
from eks_harness.ir.hashing import resource_id
from eks_harness.ir.plan import Node, Plan

LEASE = resource_id("lease.web", {"app": "web-app", "session": "main"})
BACKEND = resource_id("backend", {"app": "web-app", "seed": "standard", "session": "main"})
ROW_FIRST = {"by": "test_id", "args": {"id": "row"}, "nth": 0}


def branches(node: Node) -> dict[str, list[Node]]:
    assert node.branches is not None
    return node.branches


def twice(fn: Any) -> Plan:
    first = eks.plan(fn)
    second = eks.plan(fn)
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    assert [node.id for node, _ in first.walk()] == [node.id for node, _ in second.walk()]
    return first


def snippet_flow() -> Any:
    results: dict[str, Ref[Any]] = {}

    @eks.flow(app="web-app", platform="web", seed="standard")
    def snippet(s: eks.Session) -> Any:
        with eks.when(s.ui.exists("#empty-state")):
            s.ui.tap("#create")
        with eks.otherwise():
            s.ui.tap(by.test_id("row").first)

        with eks.each(s.ui.state("invoices")[:3]) as inv:
            s.ui.nav(f"/tenant/invoice/{inv.id}")
            s.ui.fill("#note", f"fatura-{inv.number}")

        with eks.parallel():                     # independent lanes, e.g. web and api
            s.ui.nav("/tenant/invoice/list")
            s.op("api.get", path="/api/invoices")
            s.ui.tap("#refresh")

        @eks.dynamic                              # escape hatch: real values, returns a sub-plan
        def per_row(n=s.ui.count("#row")):  # noqa: B008
            return [s.ui.tap(by.test_id("row").nth(i)) for i in range(n)]  # pyright: ignore[reportArgumentType]

        results["per_row"] = per_row
        return {"rows": per_row}

    return snippet, results


def test_design_control_flow_snippet_structure() -> None:
    snippet, results = snippet_flow()
    plan = twice(snippet)
    exists, when, state, each, parallel, count, dynamic = plan.nodes

    assert (exists.op, exists.out, exists.args) == ("ui.exists", "bool",
                                                    {"target": {"by": "test_id", "args": {"id": "empty-state"}}})
    assert when.op == "ctl.when"
    assert when.args == {"cond": {"$ref": exists.id, "path": []}}
    assert list(branches(when)) == ["then", "else"]
    (create,) = branches(when)["then"]
    (first_row,) = branches(when)["else"]
    assert create.args["target"] == {"by": "test_id", "args": {"id": "create"}}
    assert first_row.args["target"] == ROW_FIRST
    assert when.after == [exists.id]

    assert (state.op, state.args) == ("ui.state", {"path": "invoices"})
    assert each.op == "ctl.each"
    assert each.args == {"over": {"$ref": state.id, "path": [{"slice": [None, 3, None]}]}}
    nav, fill = branches(each)["body"]
    assert nav.args == {"route": {"$fmt": ["/tenant/invoice/", {"$item": each.id, "path": ["id"]}]}}
    assert fill.args["value"] == {"$fmt": ["fatura-", {"$item": each.id, "path": ["number"]}]}
    assert nav.after == []
    assert fill.after == [nav.id]

    assert parallel.op == "ctl.parallel"
    lane_nav, api, refresh = branches(parallel)["body"]
    assert (lane_nav.uses, api.uses, refresh.uses) == ([LEASE], [BACKEND], [LEASE])
    assert (api.op, api.args["path"]) == ("api.get", "/api/invoices")
    assert api.after == []
    assert refresh.after == [lane_nav.id]

    assert (count.op, count.out) == ("ui.count", "int")
    assert dynamic.op == "ctl.dynamic"
    assert dynamic.out == "json"
    assert dynamic.args["inputs"] == {"n": {"$ref": count.id, "path": []}}
    fn = cast("dict[str, Any]", dynamic.args["fn"])
    assert fn["name"] == "per_row"
    assert fn["params"] == ["n"]
    assert fn["freevars"] == {"s": {"kind": "session", "value": "main"}}
    assert "by" in fn["globals"]
    assert set(dynamic.uses) == {LEASE, BACKEND}
    assert type_of(results["per_row"]) == "json"
    assert plan.outputs == {"rows": {"$ref": dynamic.id, "path": []}}


def test_item_refs_appear_only_inside_the_each_body() -> None:
    snippet, _results = snippet_flow()
    plan = eks.plan(snippet)
    each = next(node for node in plan.nodes if node.op == "ctl.each")
    inside = {node.id for node, ancestors in plan.walk() if each in ancestors}
    for node, _ancestors in plan.walk():
        if "$item" in json.dumps(node.args):
            assert node.id in inside


def test_literal_each_expands_copies_with_suffixed_ids() -> None:
    @eks.flow(app="web-app", platform="web")
    def literal(s: eks.Session) -> None:
        with eks.each(["Kadıköy", "Beşiktaş"]) as branch:
            s.ui.tap(f"text={branch}")
            s.ui.nav(f"/tenant/branches?name={branch}")

    plan = twice(literal)
    tap0, nav0, tap1, nav1 = plan.nodes
    assert tap0.id.endswith(".0")
    assert tap1.id == tap0.id[:-2] + ".1"
    assert nav1.id == nav0.id[:-2] + ".1"
    assert tap0.args["target"] == {"by": "text", "args": {"text": "Kadıköy", "exact": True}}
    assert nav1.args["route"] == "/tenant/branches?name=Beşiktaş"
    assert tap1.after == [nav0.id]


def test_when_on_a_read_inside_each_and_nested_macro() -> None:
    @eks.macro(namespace="snippet")
    def open_row(s: eks.Session, row: Any) -> None:
        s.ui.nav(f"/tenant/invoice/{row.id}")

    @eks.flow(app="web-app", platform="web")
    def nested(s: eks.Session) -> None:
        with eks.each(s.ui.state("invoices"), limit=2) as inv, eks.when(s.ui.visible(f"text={inv.number}")):
            s.snippet.open_row(inv)

    state, each = twice(nested).nodes
    assert each.args == {"over": {"$ref": state.id, "path": []}, "limit": 2}
    visible, when = branches(each)["body"]
    assert visible.args["target"] == {
        "by": "text", "args": {"text": {"$fmt": [{"$item": each.id, "path": ["number"]}]}, "exact": True}}
    assert when.args == {"cond": {"$ref": visible.id, "path": []}}
    (macro,) = branches(when)["then"]
    assert macro.args["params"] == {"row": {"$item": each.id, "path": []}}
    (nav,) = branches(macro)["body"]
    assert nav.args["route"] == {"$fmt": ["/tenant/invoice/", {"$item": each.id, "path": ["id"]}]}
