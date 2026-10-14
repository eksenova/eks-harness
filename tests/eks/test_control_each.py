import hashlib
from typing import Any

import pytest

import eks
from eks import t
from eks.ref import Ref, is_item, node_of, type_of
from eks.tracing import TEMPLATE_FLAG, TraceError, current, normalize_call_text
from eks_harness.ir.hashing import derive_id
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.refs import RJson

TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


def register_values_op() -> None:
    @eks.op("demo", name="values", out="json")
    class Values(eks.Args):
        value: RJson = None

    assert Values.__name__ == "Values"


def walk(plan: Plan) -> list[Node]:
    return [node for node, _ancestors in plan.walk()]


def test_each_over_a_ref_records_a_runtime_container() -> None:
    items: list[Ref[Any]] = []

    @eks.flow(app="web-app", platform="web")
    def runtime(s: eks.Session) -> None:
        invoices = s.record_op("api.get", {"path": "/api/invoices"})
        with eks.each(invoices.items[:3]) as inv:
            items.append(inv)
            s.record_op("ui.nav", {"route": f"/tenant/invoice/{inv.id}"})
            s.record_op("ui.tap", {"target": {"by": "text", "args": {"text": inv.number}}})
        s.record_op("ui.nav", {"route": "/done"})

    invoices, loop, done = eks.plan(runtime).nodes
    assert loop.op == "ctl.each"
    assert loop.args == {"over": {"$ref": invoices.id, "path": ["items", {"slice": [None, 3, None]}]}}
    assert loop.after == [invoices.id]
    assert loop.branches is not None
    nav, tap = loop.branches["body"]
    assert nav.args["route"] == {"$fmt": ["/tenant/invoice/", {"$item": loop.id, "path": ["id"]}]}
    assert tap.args["target"] == {"by": "text", "args": {"text": {"$item": loop.id, "path": ["number"]}}}
    assert nav.after == []
    assert tap.after == [nav.id]
    assert done.after == [loop.id]
    (item,) = items
    assert is_item(item)
    assert node_of(item) == loop.id
    assert type_of(item) == "json"


def test_each_limit_and_item_types() -> None:
    register_values_op()
    types: list[str] = []

    @eks.flow(app="web-app", platform="web")
    def typed(s: eks.Session) -> None:
        names = s.ctx.record("demo.values", {"value": ["a"]}, out="list[str]")
        with eks.each(names, limit=2) as name:
            types.append(type_of(name))
            s.record_op("ui.nav", {"route": f"/n/{name}"})
        rows = s.record_op("api.get", {"path": "/api/rows"})
        with eks.each(rows) as anything:
            types.append(type_of(anything))

    _names, loop, _rows, _other = eks.plan(typed).nodes
    assert loop.args["limit"] == 2
    assert types == ["str", "json"]


@pytest.mark.parametrize("op", ["ui.count", "ui.text", "ui.exists"])
def test_each_over_a_scalar_ref_raises(op: str) -> None:
    # a count, a text or a flag is never a list, so the loop could not run
    @eks.flow(app="web-app", platform="web")
    def scalar(s: eks.Session) -> None:
        value = s.record_op(op, TAP)
        with eks.each(value):
            s.record_op("ui.nav", {"route": "/x"})

    with pytest.raises(TypeError, match="eks.each iterates over a list"):
        eks.plan(scalar)


def test_each_item_outside_its_block_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def leaked(s: eks.Session) -> None:
        rows = s.record_op("api.get", {"path": "/api/rows"})
        with eks.each(rows.items) as row:
            pass
        s.record_op("ui.nav", {"route": f"/r/{row.id}"})

    with pytest.raises(TraceError, match=r"outside its with block"):
        eks.plan(leaked)


def test_nested_runtime_each_items() -> None:
    @eks.flow(app="web-app", platform="web")
    def nested(s: eks.Session) -> None:
        groups = s.record_op("api.get", {"path": "/api/groups"})
        with eks.each(groups.items) as group, eks.each(group.members) as member:
            s.record_op("ui.nav", {"route": f"/g/{group.id}/m/{member.id}"})

    _groups, outer = eks.plan(nested).nodes
    assert outer.branches is not None
    (inner,) = outer.branches["body"]
    assert inner.args == {"over": {"$item": outer.id, "path": ["members"]}}
    assert inner.branches is not None
    (nav,) = inner.branches["body"]
    assert nav.args["route"] == {
        "$fmt": ["/g/", {"$item": outer.id, "path": ["id"]}, "/m/", {"$item": inner.id, "path": ["id"]}],
    }


def test_each_over_a_literal_list_expands_into_copies() -> None:
    @eks.flow(app="web-app", platform="web")
    def literal(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/start"})
        with eks.each(["a", "b"]) as item:
            s.record_op("ui.nav", {"route": f"/r/{item}"})
            s.record_op("api.post", {"path": "/api/items", "json": {"name": item, "tags": [item, "x"]}})
        s.record_op("ui.nav", {"route": "/end"})

    plan = eks.plan(literal)
    assert all(node.op != "ctl.each" for node in walk(plan))
    start, nav_a, post_a, nav_b, post_b, end = plan.nodes
    template_nav = nav_a.id.removesuffix(".0")
    template_post = post_a.id.removesuffix(".0")
    assert nav_a.id == f"{template_nav}.0"
    assert nav_b.id == f"{template_nav}.1"
    assert post_b.id == f"{template_post}.1"
    assert nav_a.args["route"] == "/r/a"
    assert nav_b.args["route"] == "/r/b"
    assert post_a.args["json"] == {"name": "a", "tags": ["a", "x"]}
    assert post_b.args["json"] == {"name": "b", "tags": ["b", "x"]}
    # copy 0 after the outer tail, copy 1 after the last node of copy 0, intra-copy edges remapped
    assert nav_a.after == [start.id]
    assert post_a.after == [nav_a.id]
    assert nav_b.after == [post_a.id]
    assert post_b.after == [nav_b.id]
    assert end.after == [post_b.id]


def test_literal_each_item_paths_inside_f_strings_and_dicts() -> None:
    register_values_op()
    elements = [{"id": 7, "name": "Kadıköy", "tags": ["a"]}, {"id": 9, "name": "Moda", "tags": ["b"]}]

    @eks.flow(app="web-app", platform="web")
    def paths(s: eks.Session) -> None:
        with eks.each(elements) as branch:
            s.record_op("ui.nav", {"route": f"/branches/{branch.id}?name={branch.name}"})
            s.ctx.record("demo.values", {"value": {"first_tag": branch.tags[0], "whole": branch, "count": branch.id}})

    nav0, values0, nav1, values1 = eks.plan(paths).nodes
    assert nav0.args["route"] == "/branches/7?name=Kadıköy"
    assert nav1.args["route"] == "/branches/9?name=Moda"
    assert values0.args["value"] == {"first_tag": "a", "whole": elements[0], "count": 7}
    assert values1.args["value"] == {"first_tag": "b", "whole": elements[1], "count": 9}


def test_literal_each_items_keep_their_json_types() -> None:
    register_values_op()

    @eks.flow(app="web-app", platform="web")
    def typed(s: eks.Session) -> None:
        with eks.each([1, 2.5, True, None, t("1.5s")]) as value:
            s.ctx.record("demo.values", {"value": value})

    assert [node.args.get("value") for node in eks.plan(typed).nodes] == [1, 2.5, True, None, "1.5s"]


def test_literal_each_copies_are_revalidated() -> None:
    @eks.flow(app="web-app", platform="web")
    def invalid(s: eks.Session) -> None:
        with eks.each(["ok", 3]) as route:
            s.record_op("ui.nav", {"route": route})

    with pytest.raises(ValueError, match="route"):
        eks.plan(invalid)


def test_literal_each_limit() -> None:
    @eks.flow(app="web-app", platform="web")
    def limited(s: eks.Session) -> None:
        with eks.each(("a", "b", "c"), limit=2) as item:
            s.record_op("ui.nav", {"route": f"/{item}"})

    assert [node.args["route"] for node in eks.plan(limited).nodes] == ["/a", "/b"]


@pytest.mark.parametrize("limit", [0, -1, True])
def test_literal_each_invalid_limit_raises(limit: int) -> None:
    @eks.flow(app="web-app", platform="web")
    def bad(s: eks.Session) -> None:
        with eks.each(["a"], limit=limit):
            pass

    with pytest.raises(ValueError, match="limit"):
        eks.plan(bad)


def test_literal_each_over_an_empty_list_records_nothing() -> None:
    @eks.flow(app="web-app", platform="web")
    def empty(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/a"})
        with eks.each([]) as item:
            s.record_op("ui.nav", {"route": f"/{item}"})
        s.record_op("ui.nav", {"route": "/b"})

    first, last = eks.plan(empty).nodes
    assert last.after == [first.id]


@pytest.mark.parametrize("over", ["abc", b"abc", {"a": 1}, 3, {1, 2}])
def test_each_over_unsupported_values_raises_type_error(over: Any) -> None:
    @eks.flow(app="web-app", platform="web")
    def unsupported(s: eks.Session) -> None:
        with eks.each(over):
            pass

    with pytest.raises(TypeError, match=r"eks\.each takes"):
        eks.plan(unsupported)


def test_literal_each_elements_must_be_json() -> None:
    @eks.flow(app="web-app", platform="web")
    def objects(s: eks.Session) -> None:
        with eks.each([object()]):
            pass

    with pytest.raises(TypeError):
        eks.plan(objects)


def test_literal_each_elements_with_refs_raise() -> None:
    @eks.flow(app="web-app", platform="web")
    def refs(s: eks.Session) -> None:
        count = s.record_op("ui.count", TAP)
        with eks.each([count]):
            pass

    with pytest.raises(TypeError, match="holds refs"):
        eks.plan(refs)


def test_literal_each_template_refs_and_containers_are_copied() -> None:
    @eks.flow(app="web-app", platform="web")
    def template(s: eks.Session) -> None:
        with eks.each(["a", "b"]) as item:
            text = s.record_op("ui.text", {"target": {"by": "test_id", "args": {"id": f"in-{item}"}}})
            s.record_op("ui.fill", {"target": {"by": "test_id", "args": {"id": "out"}}, "value": text})
            with eks.when(s.record_op("ui.exists", TAP)):
                s.record_op("ui.nav", {"route": f"/{item}/{text}"})

    plan = eks.plan(template)
    text0, fill0, _exists0, when0, text1, fill1, exists1, when1 = plan.nodes
    assert fill0.args["value"] == {"$ref": text0.id, "path": []}
    assert fill1.args["value"] == {"$ref": text1.id, "path": []}
    assert fill1.after == [text1.id]
    assert text1.after == [when0.id]
    assert when1.args == {"cond": {"$ref": exists1.id, "path": []}}
    assert when0.branches is not None
    assert when1.branches is not None
    (nav0,) = when0.branches["then"]
    (nav1,) = when1.branches["then"]
    assert nav0.args["route"] == {"$fmt": ["/a/", {"$ref": text0.id, "path": []}]}
    assert nav1.args["route"] == {"$fmt": ["/b/", {"$ref": text1.id, "path": []}]}
    assert nav0.id.endswith(".0")
    assert nav1.id == nav0.id.removesuffix(".0") + ".1"
    assert len({node.id for node in walk(plan)}) == len(walk(plan))


def test_literal_each_with_a_runtime_each_inside() -> None:
    @eks.flow(app="web-app", platform="web")
    def mixed(s: eks.Session) -> None:
        rows = s.record_op("api.get", {"path": "/api/rows"})
        with eks.each(["x", "y"]) as prefix, eks.each(rows.items) as row:
            s.record_op("ui.nav", {"route": f"/{prefix}/{row.id}"})

    _rows, loop0, loop1 = eks.plan(mixed).nodes
    assert loop0.branches is not None
    assert loop1.branches is not None
    (nav0,) = loop0.branches["body"]
    (nav1,) = loop1.branches["body"]
    assert nav0.args["route"] == {"$fmt": ["/x/", {"$item": loop0.id, "path": ["id"]}]}
    assert nav1.args["route"] == {"$fmt": ["/y/", {"$item": loop1.id, "path": ["id"]}]}


def test_nested_literal_each_suffixes_twice() -> None:
    @eks.flow(app="web-app", platform="web")
    def grid(s: eks.Session) -> None:
        with eks.each(["a", "b"]) as row, eks.each([1, 2]) as col:
            s.record_op("ui.nav", {"route": f"/{row}/{col}"})

    nodes = eks.plan(grid).nodes
    assert [node.args["route"] for node in nodes] == ["/a/1", "/a/2", "/b/1", "/b/2"]
    base = nodes[0].id.removesuffix(".0.0")
    assert [node.id for node in nodes] == [f"{base}.0.0", f"{base}.1.0", f"{base}.0.1", f"{base}.1.1"]
    assert [node.after for node in nodes[1:]] == [[nodes[0].id], [nodes[1].id], [nodes[2].id]]


def test_template_flag_and_leaked_template_refs() -> None:
    flags: list[bool] = []
    leaked: list[Ref[Any]] = []

    @eks.flow(app="web-app", platform="web")
    def flagged(s: eks.Session) -> None:
        with eks.each(["a"]):
            flags.append(current().has_flag(TEMPLATE_FLAG))
            leaked.append(s.record_op("ui.count", TAP))
        flags.append(current().has_flag(TEMPLATE_FLAG))
        s.record_op("ui.fill", {"target": {"by": "test_id", "args": {"id": "x"}}, "value": f"{leaked[0]}"})

    with pytest.raises(TraceError, match="not recorded"):
        eks.plan(flagged)
    assert flags == [True, False]


def test_returning_a_leaked_template_ref_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def leaking(s: eks.Session) -> Any:
        leaked: list[Ref[Any]] = []
        with eks.each(["a", "b"]):
            leaked.append(s.record_op("ui.count", TAP))
        return {"count": leaked[0]}

    with pytest.raises(TraceError, match="returned value references node"):
        eks.plan(leaking)


def test_returning_a_runtime_each_item_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def item_out(s: eks.Session) -> Any:
        rows = s.record_op("api.get", {"path": "/api/rows"})
        items: list[Ref[Any]] = []
        with eks.each(rows.items) as item:
            items.append(item)
        return items[0]

    with pytest.raises(TraceError, match="item of eks.each"):
        eks.plan(item_out)


def test_literal_each_missing_item_path_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def missing(s: eks.Session) -> None:
        with eks.each([{"id": 1}, {"name": "x"}]) as item:
            s.record_op("ui.nav", {"route": f"/{item.id}"})

    with pytest.raises(TraceError, match="element 1"):
        eks.plan(missing)


def test_literal_each_ids_are_stable() -> None:
    @eks.flow(app="web-app", platform="web")
    def stable(s: eks.Session) -> None:
        with eks.each(["a", "b"]) as item:
            s.record_op("ui.nav", {"route": f"/{item}"}, id_key="nav")

    first = eks.plan(stable)
    second = eks.plan(stable)
    assert [node.id for node in first.nodes] == [node.id for node in second.nodes]
    assert first.hash == second.hash
    # the template id is derived like a ctl.each node at the each call; its body scope keys the copies
    call = hashlib.sha256(normalize_call_text('eks.each(["a", "b"])').encode()).hexdigest()[:12]
    template_id = derive_id("n", f"flow:stable/tests/eks/test_control_each.py:{stable.__qualname__}:{call}/ctl.each#0")
    template_node = derive_id("n", f"{template_id}/body/nav#0")
    assert [node.id for node in first.nodes] == [f"{template_node}.0", f"{template_node}.1"]
