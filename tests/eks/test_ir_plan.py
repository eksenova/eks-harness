from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from eks_harness.ir.plan import (
    Binding,
    Clock,
    FunctionSource,
    Node,
    Plan,
    PlanError,
    Requirement,
    Resource,
    SessionSpec,
    SourceInfo,
    normalize_now,
)


def nid(suffix: str) -> str:
    return "n" + suffix.rjust(10, "0")


def build_plan() -> Plan:
    # n1, when(n2){then: n3, each(n4){body: n5}; else: n6}, n7
    each = Node(
        id=nid("4"),
        op="ctl.each",
        args={"over": {"$ref": nid("1"), "path": ["items"]}},
        branches={"body": [Node(id=nid("5"), op="ui.nav", args={"route": {"$item": nid("4"), "path": ["id"]}})]},
    )
    when = Node(
        id=nid("2"),
        op="ctl.when",
        args={"cond": {"$ref": nid("1"), "path": ["ok"]}},
        after=[nid("1")],
        branches={
            "then": [Node(id=nid("3"), op="ui.tap", args={"target": "#a"}), each],
            "else": [Node(id=nid("6"), op="ui.tap", args={"target": "#b"})],
        },
    )
    return Plan(
        kind="flow",
        name="subeler",
        params={"tenant": "harman"},
        clock=Clock(now="2026-01-15T09:00+03:00", seed=2026),
        session=SessionSpec(app="web-app", platform="web", seed="standard", persona="harman-admin"),
        resources=[
            Resource(id="rlease01", kind="lease.web", app="web-app", session="main"),
            Resource(id="rbacken0", kind="backend", definition="eksenapi", seed="standard"),
        ],
        nodes=[
            Node(id=nid("1"), op="api.get", args={"path": "/api/tenant-branches"}, uses=["rbacken0"], out="json"),
            when,
            Node(id=nid("7"), op="ui.nav", args={"route": "/done"}, uses=["rlease01"], after=[nid("2")]),
        ],
        outputs={"branches": {"$ref": nid("1"), "path": []}},
        personas=[],
        requires=[Requirement(plugin="eksenova", version="1.0")],
        base="flows",
        tags=["subeler"],
        source=SourceInfo(file="flows/subeler.py", qualname="subeler", line=3),
    )


def test_plan_json_uses_schema_key_and_round_trips() -> None:
    plan = build_plan()
    text = plan.to_json()
    data = json.loads(text)
    assert data["schema"] == "eks/plan@1"
    assert "schema_" not in data
    assert data["clock"]["now"] == "2026-01-15T09:00:00+03:00"
    assert "session" in data
    assert "label" not in data["nodes"][0]
    assert "branches" not in data["nodes"][0]
    assert data["nodes"][1]["branches"]["else"][0]["id"] == nid("6")
    again = Plan.from_json(text)
    assert again == plan
    assert again.to_json() == text
    assert Plan.from_json(text.encode("utf-8")) == plan


def test_to_json_indent_none_is_compact() -> None:
    text = build_plan().to_json(indent=None)
    assert "\n" not in text
    assert json.loads(text)["name"] == "subeler"


def test_plan_json_omits_none_values() -> None:
    plan = Plan(kind="seed", name="standard")
    data = json.loads(plan.to_json())
    assert data == {
        "schema": "eks/plan@1",
        "kind": "seed",
        "name": "standard",
        "params": {},
        "clock": {"seed": 0, "cues": {}},
        "resources": [],
        "nodes": [],
        "personas": [],
        "requires": [],
        "base": ".",
        "tags": [],
        "hash": "",
    }


def test_from_json_validates() -> None:
    with pytest.raises(ValidationError):
        Plan.from_json('{"schema": "eks/plan@2", "kind": "flow", "name": "x"}')
    with pytest.raises(ValidationError):
        Plan.from_json('{"kind": "flow", "name": "x", "bogus": 1}')
    with pytest.raises(ValidationError, match="unknown key 'schema_'"):
        # The schema field is only addressable by its JSON key.
        Plan.from_json('{"schema_": "eks/plan@1", "kind": "flow", "name": "x"}')
    with pytest.raises(ValidationError, match="unknown key 'schema_'"):
        Plan.model_validate({"schema_": "eks/plan@1", "kind": "flow", "name": "x"})


def test_walk_order_and_ancestors() -> None:
    plan = build_plan()
    order = [(node.id, tuple(a.id for a in ancestors)) for node, ancestors in plan.walk()]
    assert order == [
        (nid("1"), ()),
        (nid("2"), ()),
        (nid("3"), (nid("2"),)),
        (nid("4"), (nid("2"),)),
        (nid("5"), (nid("2"), nid("4"))),
        (nid("6"), (nid("2"),)),
        (nid("7"), ()),
    ]


def test_node_and_resource_lookup() -> None:
    plan = build_plan()
    assert plan.node(nid("5")).op == "ui.nav"
    assert plan.node(nid("6")).args == {"target": "#b"}
    assert plan.resource("rbacken0").definition == "eksenapi"
    with pytest.raises(PlanError, match=nid("9")):
        plan.node(nid("9"))
    with pytest.raises(PlanError, match="rmissing"):
        plan.resource("rmissing")


def test_normalize_now() -> None:
    assert normalize_now("2026-01-15T09:00+03:00") == "2026-01-15T09:00:00+03:00"
    assert normalize_now("2026-01-15T09:00:30.999+00:00") == "2026-01-15T09:00:30+00:00"
    assert normalize_now("2026-01-15T06:00:00Z") == "2026-01-15T06:00:00+00:00"
    assert normalize_now(None) is None


@pytest.mark.parametrize("value", ["2026-01-15T09:00", "2026-01-15", "tomorrow", ""])
def test_normalize_now_errors(value: str) -> None:
    with pytest.raises(ValueError, match="2026|offset|ISO"):
        normalize_now(value)


def test_clock_normalizes_now() -> None:
    assert Clock(now="2026-01-15T09:00+03:00").now == "2026-01-15T09:00:00+03:00"
    with pytest.raises(ValidationError):
        Clock(now="2026-01-15T09:00")


@pytest.mark.parametrize("node_id", ["n1", "n00000000001", "N0000000001", "x0000000001", "n000000000-"])
def test_node_id_pattern_rejects(node_id: str) -> None:
    with pytest.raises(ValidationError):
        Node(id=node_id, op="ui.tap")


def test_node_id_pattern_accepts_copy_suffixes() -> None:
    assert Node(id="n0123456789.0", op="ui.tap").id == "n0123456789.0"
    assert Node(id="n0123456789.2.11", op="ui.tap").id == "n0123456789.2.11"


def test_resource_id_pattern() -> None:
    assert Resource(id="r0abc123", kind="backend").kind == "backend"
    with pytest.raises(ValidationError):
        Resource(id="r1", kind="backend")


def test_node_defaults() -> None:
    node = Node(id=nid("1"), op="ui.tap")
    assert node.v == "1"
    assert node.args == {}
    assert node.uses == []
    assert node.after == []
    assert node.out == "none"
    assert node.branches is None
    assert node.hash == ""


def test_function_source_model() -> None:
    fn = FunctionSource(
        name="per_row",
        source="def per_row(n):\n    return []\n",
        hash="sha256:" + "0" * 64,
        params=("n",),
        freevars={"s": Binding(kind="session", value="main")},
    )
    assert fn.model_dump(mode="json")["freevars"] == {"s": {"kind": "session", "value": "main"}}
    with pytest.raises(ValidationError):
        FunctionSource(name="x", source="", hash="md5:abc")
