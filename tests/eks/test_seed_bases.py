from __future__ import annotations

import json
from typing import Any

import pytest

import eks
from eks.tracing import TraceError
from eks.workspace import seed_info, seed_personas
from eks_harness.engine.validator import validate
from eks_harness.ir.ops.data import ResourceRef
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry

NOW = "2026-01-15T09:00+03:00"


def register_kinds() -> None:
    @eks.resource("federation", description="A reseller federation")
    class Federation(eks.Resource):
        type: str

    @eks.resource("tenant", description="A jeweller tenant")
    class Tenant(eks.Resource):
        name: str
        reseller: ResourceRef | None = None

    @eks.resource("persona", parent="tenant", persona=True, description="A tenant user")
    class Persona(eks.Resource):
        role: str

    @eks.resource("invoice", parent="tenant", keyed=False, description="One invoice of a tenant")
    class Invoice(eks.Resource):
        state: str


def register_standard() -> None:
    register_kinds()

    @eks.seed("standard", backend="eksenapi", now=NOW, seed=2026)
    def standard(d: Any) -> None:
        bayi = d.federation("anadolu-bayi", type="Reseller")
        harman = d.tenant("harman", name="Harman Kuyumculuk", reseller=bayi)
        harman.persona("harman-admin", role="admin")
        harman.invoice(state="Draft")
        harman.invoice(state="Confirmed")


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def test_an_extension_cannot_declare_a_base_resource_again(eks_registry: Registry) -> None:
    register_standard()

    @eks.seed("collide", backend="eksenapi", now=NOW, seed=2026)
    def collide(d: Any) -> None:
        d.extend(base="standard")
        d.federation("anadolu-bayi", type="Reseller")

    with pytest.raises(TraceError, match=r"already created by base seed 'standard'.*d\.base\('federation:anadolu-bayi'\)"):
        eks.plan(collide)


def test_an_extension_cannot_reuse_a_base_persona_key(eks_registry: Registry) -> None:
    register_standard()

    @eks.seed("persona_again", backend="eksenapi", now=NOW, seed=2026)
    def persona_again(d: Any) -> None:
        d.extend(base="standard")
        moda = d.tenant("moda", name="Moda Kuyumculuk")
        moda.persona("harman-admin", role="cashier")

    with pytest.raises(TraceError, match="persona 'harman-admin' is already created by base seed 'standard'"):
        eks.plan(persona_again)


def test_base_handles_add_children_without_redeclaring(eks_registry: Registry) -> None:
    register_standard()

    @eks.seed("feature", backend="eksenapi", now=NOW, seed=2026)
    def feature(d: Any) -> None:
        d.extend(base="standard")
        harman = d.base("tenant:harman")
        assert d.base("tenant:harman") is harman
        harman.persona("harman-cashier", role="cashier")
        harman.invoice(state="Cancelled")
        d.tenant("moda", name="Moda Kuyumculuk", reseller=d.base("federation:anadolu-bayi"))

    plan = eks.plan(feature)
    (extend,) = by_op(plan, "data.extend")
    lookups = {node.args["path"]: node for node in by_op(plan, "data.base")}
    assert set(lookups) == {"tenant:harman", "federation:anadolu-bayi"}
    harman = lookups["tenant:harman"]
    assert harman.out == "resource:tenant"
    assert harman.args == {"base": "standard", "kind": "tenant", "key": "harman", "path": "tenant:harman"}
    assert extend.id in harman.after
    paths = [node.args["path"] for node in by_op(plan, "data.create")]
    assert paths == ["tenant:harman/persona:harman-cashier", "tenant:harman/invoice#2", "tenant:moda"]
    assert plan.personas == ["harman-cashier"]
    assert seed_personas("feature") == ["harman-admin", "harman-cashier"]
    assert validate(plan, seeds=seed_info).issues == ()


def test_a_base_handle_needs_a_resource_the_base_creates(eks_registry: Registry) -> None:
    register_standard()

    @eks.seed("missing", backend="eksenapi", now=NOW, seed=2026)
    def missing(d: Any) -> None:
        d.extend(base="standard")
        d.base("tenant:harmann")

    with pytest.raises(TraceError, match="did you mean 'tenant:harman'"):
        eks.plan(missing)

    @eks.seed("unextended", backend="eksenapi", now=NOW, seed=2026)
    def unextended(d: Any) -> None:
        d.base("tenant:harman")

    with pytest.raises(TraceError, match=r"call d\.extend\(base\) first"):
        eks.plan(unextended)


PATH_CODES = ("seed.path", "seed.persona")


def test_the_validator_compares_creates_with_the_base_chain(eks_registry: Registry) -> None:
    register_standard()

    @eks.seed("edited", backend="eksenapi", now=NOW, seed=2026)
    def edited(d: Any) -> None:
        d.extend(base="standard")
        harman = d.base("tenant:harman")
        harman.persona("harman-cashier", role="cashier")

    data: dict[str, Any] = json.loads(eks.plan(edited).to_json())
    create = next(node for node in data["nodes"] if node["op"] == "data.create")
    lookup = next(node for node in data["nodes"] if node["op"] == "data.base")
    create["args"]["key"] = "harman-admin"
    create["args"]["path"] = "tenant:harman/persona:harman-admin"
    lookup["args"]["path"] = "tenant:nowhere"
    issues = validate(Plan.model_validate(data), seeds=seed_info).issues
    found = [(issue.code, issue.field) for issue in issues if issue.code not in ("plan.hash", *PATH_CODES)]
    assert found == [("seed.resource", "path"), ("seed.resource", "path")]
    # the persona key changed without Plan.personas; the lookup's path no longer names its tenant key, and the
    # persona's path no longer follows from that parent
    assert sorted((issue.code, issue.field) for issue in issues if issue.code in PATH_CODES) == [
        ("seed.path", "path"), ("seed.path", "path"), ("seed.persona", None)]

    create["args"]["path"] = "tenant:harman/persona:harman-admin-2"
    lookup["args"]["path"] = "tenant:harman"
    lookup["args"]["base"] = "other"
    found = [(issue.code, issue.field) for issue in validate(Plan.model_validate(data), seeds=seed_info).issues
             if issue.code not in ("plan.hash", *PATH_CODES)]
    assert found == [("seed.resource", "base"), ("seed.resource", "key")]
