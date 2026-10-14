from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from eks_harness.ir.hashing import (
    CROCKFORD,
    b32,
    canonical_json,
    derive_id,
    derived_seed,
    digest,
    hash_plan,
    node_hash,
    plan_hash,
    resource_hash,
    resource_id,
)
from eks_harness.ir.plan import (
    Clock,
    Node,
    Plan,
    PlanError,
    Resource,
    SessionSpec,
    SourceInfo,
)
from eks_harness.ir.registry import Registry

HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
LEASE = "rlease01"
BACKEND = "rbacken0"


def ids(prefix: str) -> Callable[[str], str]:
    # Builds node ids with a plan-specific prefix so two plans can have different ids for the same content.
    return lambda suffix: "n" + (prefix + suffix).rjust(10, "0")


def build(
    nid: Callable[[str], str],
    *,
    name: str = "subeler",
    tags: tuple[str, ...] = ("subeler",),
    route: str = "/tenant/settings/branches",
    query: int = 50,
    src: str | None = "flows/subeler.py:3",
    label: str | None = None,
) -> Plan:
    n1, n2, n3, n4, n5, n6 = (nid(str(i)) for i in range(1, 7))
    return Plan(
        kind="flow",
        name=name,
        params={"tenant": "harman"},
        clock=Clock(now="2026-01-15T09:00+03:00", seed=2026),
        session=SessionSpec(app="web-app", platform="web", seed="standard"),
        resources=[
            Resource(id=LEASE, kind="lease.web", app="web-app", session="main"),
            Resource(id=BACKEND, kind="backend", definition="eksenapi", seed="standard", session="main"),
        ],
        nodes=[
            Node(
                id=n1,
                op="api.get",
                args={"path": "/api/tenant-branches", "query": {"MaxResultCount": query}},
                uses=[BACKEND],
                out="json",
                src=src,
                label=label,
            ),
            Node(id=n2, op="ui.nav", args={"route": route}, uses=[LEASE], after=[n1], src=src),
            Node(
                id=n3,
                op="ui.nav",
                args={"route": {"$fmt": ["/x?id=", {"$ref": n1, "path": ["items", {"find": {"name": "Kadıköy"}}, "id"]}]}},
                uses=[LEASE],
                after=[n2, n1],
            ),
            Node(
                id=n4,
                op="ctl.when",
                args={"cond": {"$ref": n1, "path": ["items", {"len": True}]}},
                after=[n3],
                branches={
                    "then": [Node(id=n5, op="ui.tap", args={"target": {"kind": "test_id", "id": "row"}}, uses=[LEASE])],
                    "else": [],
                },
            ),
            Node(id=n6, op="ui.nav", args={"route": "/done"}, uses=[LEASE], after=[n4]),
        ],
        outputs={"branches": {"$ref": n1, "path": []}},
        tags=list(tags),
        source=SourceInfo(file="flows/subeler.py", qualname=name, line=3),
    )


A = ids("a")
B = ids("b")


def root_hashes(plan: Plan) -> list[str]:
    return [node.hash for node in plan.nodes]


# canonical JSON and digests


def test_canonical_json_sorting_and_separators() -> None:
    assert canonical_json({"b": 1, "a": [1, 2, {"d": None, "c": True}]}) == b'{"a":[1,2,{"c":true,"d":null}],"b":1}'


def test_canonical_json_keeps_non_ascii() -> None:
    assert canonical_json({"name": "Kadıköy"}) == '{"name":"Kadıköy"}'.encode()


def test_canonical_json_dumps_models() -> None:
    class Model(BaseModel):
        b: int
        a: str | None = None

    assert canonical_json(Model(b=1)) == b'{"b":1}'


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, {"x": [math.nan]}])
def test_canonical_json_rejects_non_finite(value: Any) -> None:
    with pytest.raises(ValueError, match="finite"):
        canonical_json(value)


@pytest.mark.parametrize("value", [(1, 2), {1: "a"}, {"a": {1, 2}}, b"x", object(), {"a": [(1,)]}])
def test_canonical_json_rejects_non_json(value: Any) -> None:
    with pytest.raises(TypeError):
        canonical_json(value)


def test_digest() -> None:
    assert digest(b"abc") == "sha256:" + hashlib.sha256(b"abc").hexdigest()
    assert HASH.match(digest(b""))


# ids


def reference_b32(data: bytes, length: int) -> str:
    bits = "".join(f"{byte:08b}" for byte in data)[: 5 * length]
    return "".join(CROCKFORD[int(bits[i : i + 5], 2)] for i in range(0, len(bits), 5))


def test_crockford_alphabet() -> None:
    assert len(CROCKFORD) == 32
    assert len(set(CROCKFORD)) == 32
    assert not set("ilou") & set(CROCKFORD)
    assert CROCKFORD == CROCKFORD.lower()


@pytest.mark.parametrize("length", [0, 1, 7, 10, 51])
def test_b32_matches_reference(length: int) -> None:
    data = hashlib.sha256(b"eks").digest()
    assert b32(data, length) == reference_b32(data, length)
    assert len(b32(data, length)) == length


def test_b32_known_values() -> None:
    assert b32(b"\x00\x00", 3) == "000"
    assert b32(b"\xff\xff", 3) == "zzz"
    assert b32(b"\x08", 1) == "1"


def test_b32_needs_enough_bits() -> None:
    with pytest.raises(ValueError, match="base32"):
        b32(b"\x00", 2)


def test_derive_id() -> None:
    node = derive_id("n", "flow:subeler/flows/subeler.py:subeler:abc/ui.nav#0")
    assert re.fullmatch(r"n[0-9a-z]{10}", node)
    assert set(node[1:]) <= set(CROCKFORD)
    assert node == derive_id("n", "flow:subeler/flows/subeler.py:subeler:abc/ui.nav#0")
    assert node != derive_id("n", "flow:subeler/flows/subeler.py:subeler:abc/ui.nav#1")
    assert len(derive_id("r", "x", 7)) == 8
    assert derive_id("n", "k") == "n" + reference_b32(hashlib.sha256(b"k").digest(), 10)


def test_resource_id() -> None:
    rid = resource_id("backend", {"app": "web-app", "definition": "eksenapi", "seed": None})
    assert re.fullmatch(r"r[0-9a-z]{7}", rid)
    assert rid == resource_id("backend", {"definition": "eksenapi", "app": "web-app"})
    assert rid != resource_id("backend", {"app": "web-app", "definition": "eksenapi", "seed": "standard"})
    assert rid != resource_id("lease.web", {"app": "web-app", "definition": "eksenapi"})
    expected_key = canonical_json({"kind": "backend", "app": "web-app", "definition": "eksenapi"}).decode()
    assert rid == derive_id("r", expected_key, 7)
    # The id is valid for the Resource model.
    assert Resource(id=rid, kind="backend").id == rid


def test_resource_id_rejects_kind_in_identity() -> None:
    with pytest.raises(ValueError, match="kind"):
        resource_id("backend", {"kind": "other"})


def test_resource_hash_ignores_id() -> None:
    one = Resource(id="r0000001", kind="backend", definition="eksenapi")
    two = Resource(id="r0000002", kind="backend", definition="eksenapi")
    three = Resource(id="r0000001", kind="backend", definition="other")
    assert resource_hash(one) == resource_hash(two)
    assert resource_hash(one) != resource_hash(three)
    assert HASH.match(resource_hash(one))


# node hashes


def test_node_hash_ignores_id_src_label_and_hash() -> None:
    base = Node(id="n0000000001", op="ui.tap", args={"target": "#a"}, uses=[LEASE])
    other = Node(id="n0000000002", op="ui.tap", args={"target": "#a"}, uses=[LEASE], src="x.py:9", label="tap",
                 hash="sha256:" + "1" * 64)
    resources = {LEASE: "sha256:lease"}
    assert node_hash(base, refs={}, resources=resources) == node_hash(other, refs={}, resources=resources)


def test_node_hash_covers_op_version_executor_out_uses() -> None:
    resources = {LEASE: "sha256:lease", BACKEND: "sha256:backend"}
    base = Node(id="n0000000001", op="ui.tap", args={"target": "#a"}, uses=[LEASE])
    reference = node_hash(base, refs={}, resources=resources)
    variants = [
        base.model_copy(update={"op": "ui.hover"}),
        base.model_copy(update={"v": "2"}),
        base.model_copy(update={"out": "json"}),
        base.model_copy(update={"uses": [BACKEND]}),
        base.model_copy(update={"args": {"target": "#b"}}),
    ]
    for variant in variants:
        assert node_hash(variant, refs={}, resources=resources) != reference
    assert node_hash(base, refs={}, resources=resources, executor_version="2") != reference


def test_node_hash_after_is_order_independent() -> None:
    refs = {"n0000000001": "sha256:a", "n0000000002": "sha256:b"}
    one = Node(id="n0000000003", op="ui.tap", after=["n0000000001", "n0000000002"])
    two = Node(id="n0000000003", op="ui.tap", after=["n0000000002", "n0000000001"])
    assert node_hash(one, refs=refs, resources={}) == node_hash(two, refs=refs, resources={})


def test_node_hash_replaces_refs_by_hashes() -> None:
    node = Node(id="n0000000003", op="ui.nav", args={"route": {"$ref": "n0000000001", "path": ["id"]}})
    renamed = Node(id="n0000000003", op="ui.nav", args={"route": {"$ref": "n000000000z", "path": ["id"]}})
    assert node_hash(node, refs={"n0000000001": "sha256:x"}, resources={}) == node_hash(
        renamed, refs={"n000000000z": "sha256:x"}, resources={}
    )
    assert node_hash(node, refs={"n0000000001": "sha256:x"}, resources={}) != node_hash(
        node, refs={"n0000000001": "sha256:y"}, resources={}
    )


@pytest.mark.parametrize(
    ("node", "message"),
    [
        (Node(id="n0000000003", op="ui.nav", args={"route": {"$ref": "n0000000009", "path": []}}), "n0000000009"),
        (Node(id="n0000000003", op="ui.nav", after=["n0000000009"]), "n0000000009"),
        (Node(id="n0000000003", op="ui.nav", uses=["r0000009"]), "r0000009"),
        (Node(id="n0000000003", op="ui.nav", args={"x": {"$item": "n00000000ea", "path": []}}), r"\$item"),
        (Node(id="n0000000003", op="ctl.parallel", branches={"body": [Node(id="n0000000004", op="ui.tap")]}),
         "n0000000004"),
    ],
)
def test_node_hash_missing_inputs_raise(node: Node, message: str) -> None:
    with pytest.raises(PlanError, match=message):
        node_hash(node, refs={}, resources={})


# plan hashing


def test_hash_plan_fills_every_hash() -> None:
    plan = build(A)
    returned = hash_plan(plan)
    assert returned is plan
    assert HASH.match(plan.hash)
    for node, _ancestors in plan.walk():
        assert HASH.match(node.hash), node.id
    assert len({node.hash for node, _ in plan.walk()}) == 6


def test_hash_plan_children_feed_container_hash() -> None:
    plan = hash_plan(build(A))
    changed = build(A)
    branches = changed.nodes[3].branches
    assert branches is not None
    branches["then"][0].args = {"target": {"kind": "test_id", "id": "other"}}
    hash_plan(changed)
    assert changed.nodes[3].hash != plan.nodes[3].hash
    assert changed.nodes[4].hash != plan.nodes[4].hash
    assert root_hashes(changed)[:3] == root_hashes(plan)[:3]


def test_different_ids_same_content_give_same_hashes() -> None:
    one = hash_plan(build(A))
    two = hash_plan(build(B))
    assert [n.id for n, _ in one.walk()] != [n.id for n, _ in two.walk()]
    assert [n.hash for n, _ in one.walk()] == [n.hash for n, _ in two.walk()]
    assert one.hash == two.hash


def test_src_and_label_do_not_change_hashes() -> None:
    one = hash_plan(build(A))
    two = hash_plan(build(A, src="flows/subeler.py:40", label="branches"))
    assert root_hashes(one) == root_hashes(two)
    assert one.hash == two.hash


def test_changing_args_changes_lane_after_it_only() -> None:
    one = hash_plan(build(A))
    two = hash_plan(build(A, route="/tenant/settings/other"))
    h1, h2 = root_hashes(one), root_hashes(two)
    assert h1[0] == h2[0]
    assert h1[1] != h2[1]
    assert h1[2] != h2[2]
    assert h1[3] != h2[3]
    assert h1[4] != h2[4]
    assert one.hash != two.hash


def test_changing_referenced_node_changes_dependents() -> None:
    one = hash_plan(build(A))
    two = hash_plan(build(A, query=10))
    assert all(a != b for a, b in zip(root_hashes(one), root_hashes(two), strict=True))


def test_plan_hash_ignores_name_tags_source() -> None:
    one = hash_plan(build(A))
    two = hash_plan(build(A, name="renamed", tags=("x", "y")))
    assert one.hash == two.hash
    three = build(A)
    three.params = {"tenant": "beyaz"}
    assert hash_plan(three).hash != one.hash


@pytest.mark.parametrize(
    "change",
    [
        lambda p: setattr(p, "kind", "seed"),
        lambda p: setattr(p, "base", "flows"),
        lambda p: setattr(p, "personas", ["host"]),
        lambda p: setattr(p, "clock", Clock(now="2026-01-15T09:00+03:00", seed=7)),
        lambda p: setattr(p, "session", SessionSpec(app="web-app", platform="ios", seed="standard")),
        lambda p: setattr(p, "outputs", {"other": {"$ref": p.nodes[0].id, "path": ["items"]}}),
        lambda p: setattr(p, "resources", [*p.resources, Resource(id="r0000003", kind="lease.ios")]),
    ],
)
def test_plan_hash_covers_fields(change: Callable[[Plan], None]) -> None:
    reference = hash_plan(build(A)).hash
    plan = build(A)
    change(plan)
    assert hash_plan(plan).hash != reference


def test_plan_hash_ignores_resource_order() -> None:
    one = hash_plan(build(A))
    two = build(A)
    two.resources = list(reversed(two.resources))
    assert hash_plan(two).hash == one.hash


def test_plan_hash_outputs_use_node_hashes() -> None:
    one = hash_plan(build(A))
    two = hash_plan(build(B))
    assert one.outputs != two.outputs
    assert plan_hash(one) == plan_hash(two) == one.hash


def test_plan_hash_requires_hashed_nodes() -> None:
    with pytest.raises(PlanError, match="hash_plan"):
        plan_hash(build(A))


def test_plan_hash_rejects_bad_outputs() -> None:
    plan = build(A)
    plan.outputs = {"x": {"$item": "n00000000ea", "path": []}}
    with pytest.raises(PlanError, match=r"\$item"):
        hash_plan(plan)
    plan.outputs = {"x": {"$ref": "n000000000z", "path": []}}
    with pytest.raises(PlanError, match="n000000000z"):
        hash_plan(plan)


def test_hash_plan_rejects_forward_references() -> None:
    plan = build(A)
    plan.nodes[0].args = {"route": {"$ref": plan.nodes[1].id, "path": []}}
    with pytest.raises(PlanError, match=plan.nodes[1].id):
        hash_plan(plan)


def test_hash_plan_is_deterministic_byte_for_byte() -> None:
    one = hash_plan(build(A))
    two = hash_plan(build(A))
    assert one.to_json() == two.to_json()
    assert one.to_json(indent=None).encode() == two.to_json(indent=None).encode()
    assert one.hash == two.hash
    assert hash_plan(Plan.from_json(one.to_json())).hash == one.hash


def test_hash_plan_executor_versions(eks_registry: Registry) -> None:
    reference = hash_plan(build(A))
    explicit = hash_plan(build(A), executor_versions={"ui.nav": "9"})
    assert root_hashes(explicit)[0] == root_hashes(reference)[0]
    assert root_hashes(explicit)[1] != root_hashes(reference)[1]
    registered = eks_registry.op("ui.nav").executor_version
    same = hash_plan(build(A), executor_versions={"ui.nav": registered})
    assert root_hashes(same) == root_hashes(reference)


def test_hash_plan_unregistered_op_uses_empty_executor_version() -> None:
    plan = Plan(kind="flow", name="x", nodes=[Node(id="n0000000001", op="custom.unregistered_op")])
    hash_plan(plan)
    # nodes hashed by hash_plan carry the clock they depend on (here only the root seed) and, for an op the
    # registry does not know, kinds=None because its catalog kinds and presets cannot be read
    expected = node_hash(plan.nodes[0], refs={}, resources={}, executor_version="",
                         clock={"seed": 0, "kinds": None})
    assert plan.nodes[0].hash == expected


# $item depth hashing


def each_plan(nid: Callable[[str], str], *, inner_ref: str) -> Plan:
    # n1 (list); each n2 over n1.items { each n3 over $item(n2).rows { n4 uses $item(inner_ref) and $item(n3) } }
    n1, n2, n3, n4 = (nid(str(i)) for i in range(1, 5))
    target = n2 if inner_ref == "outer" else n3
    body = Node(
        id=n4,
        op="ui.nav",
        args={"route": {"$fmt": ["/x/", {"$item": target, "path": ["id"]}, "/", {"$item": n3, "path": ["n"]}]}},
        uses=[LEASE],
    )
    inner = Node(
        id=n3,
        op="ctl.each",
        args={"over": {"$item": n2, "path": ["rows"]}},
        branches={"body": [body]},
    )
    outer = Node(
        id=n2,
        op="ctl.each",
        args={"over": {"$ref": n1, "path": ["items"]}},
        after=[n1],
        branches={"body": [inner]},
    )
    return Plan(
        kind="flow",
        name="each",
        resources=[Resource(id=LEASE, kind="lease.web")],
        nodes=[Node(id=n1, op="api.get", args={"path": "/x"}, out="json"), outer],
    )


def test_item_depth_hashing_is_independent_of_each_ids() -> None:
    one = hash_plan(each_plan(A, inner_ref="outer"))
    two = hash_plan(each_plan(B, inner_ref="outer"))
    assert [n.hash for n, _ in one.walk()] == [n.hash for n, _ in two.walk()]
    assert one.hash == two.hash


def test_item_depth_distinguishes_loops() -> None:
    outer = hash_plan(each_plan(A, inner_ref="outer"))
    inner = hash_plan(each_plan(A, inner_ref="inner"))
    outer_body = [n for n, _ in outer.walk()][-1]
    inner_body = [n for n, _ in inner.walk()][-1]
    assert outer_body.hash != inner_body.hash


def test_item_depth_values() -> None:
    plan = each_plan(A, inner_ref="outer")
    body = [n for n, _ in plan.walk()][-1]
    n2, n3 = A("2"), A("3")
    resources = {LEASE: "sha256:lease"}
    expected_payload = {
        "op": "ui.nav",
        "v": "1",
        "x": "",
        "args": {"route": {"$fmt": ["/x/", {"$item": 1, "path": ["id"]}, "/", {"$item": 0, "path": ["n"]}]}},
        "uses": ["sha256:lease"],
        "after": [],
        "out": "none",
        "pin": None,
        "branches": None,
        "clock": None,
    }
    assert node_hash(body, refs={}, resources=resources, items=(n2, n3)) == digest(canonical_json(expected_payload))


def test_item_outside_each_raises() -> None:
    plan = each_plan(A, inner_ref="outer")
    plan.nodes.append(Node(id=A("9"), op="ui.nav", args={"route": {"$item": A("2"), "path": []}}))
    with pytest.raises(PlanError, match=r"\$item"):
        hash_plan(plan)


# derived seeds


def test_derived_seed() -> None:
    seed = derived_seed(2026, "n0123456789/effects/0")
    assert 0 <= seed < 2**63
    assert seed == derived_seed(2026, "n0123456789/effects/0")
    assert seed != derived_seed(2027, "n0123456789/effects/0")
    assert seed != derived_seed(2026, "n0123456789/effects/1")
    expected = int.from_bytes(hashlib.sha256(b"2026:n0123456789/effects/0").digest()[:8], "big") & (2**63 - 1)
    assert seed == expected


def test_plan_json_has_hashes_after_hashing() -> None:
    plan = hash_plan(build(A))
    data = json.loads(plan.to_json())
    assert data["hash"] == plan.hash
    assert data["nodes"][3]["branches"]["then"][0]["hash"].startswith("sha256:")
