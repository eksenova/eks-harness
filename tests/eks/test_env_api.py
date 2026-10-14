from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import t
from eks_harness.ir.ops.api import Get, Post
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry


def resource_ids(plan: Plan) -> dict[str, str]:
    return {resource.kind: resource.id for resource in plan.resources}


def ops(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ in plan.walk() if node.op == op]


def ref_dict(node: Node, *path: Any) -> dict[str, Any]:
    return {"$ref": node.id, "path": list(path)}


def test_design_4_1_api_get_and_ref_into_a_later_path(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", seed="standard", as_="harman-admin", tags=["subeler"])
    def subeler(s: eks.Session) -> dict[str, Any]:
        branches = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
        kadikoy = branches.items.find(name="Kadıköy").id
        s.api.get(f"/api/invoices?branchId={kadikoy}")
        return {"branches": branches}

    plan = eks.plan(subeler)
    rids = resource_ids(plan)
    login, listing, invoices = plan.nodes
    assert login.op == "session.as"
    assert login.args == {"persona": "harman-admin"}
    assert listing.op == "api.get"
    assert listing.args == {"path": "/api/tenant-branches", "query": {"MaxResultCount": 50}}
    assert listing.uses == [rids["backend"]]
    assert listing.out == "json"
    assert invoices.op == "api.get"
    assert invoices.args == {
        "path": {"$fmt": ["/api/invoices?branchId=", ref_dict(listing, "items", {"find": {"name": "Kadıköy"}}, "id")]},
    }
    assert invoices.after == [listing.id]
    assert invoices.uses == [rids["backend"]]
    assert plan.outputs == {"branches": ref_dict(listing)}


def test_post_keeps_the_nested_ref_under_json(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def create(s: eks.Session) -> None:
        branch = s.api.get("/api/tenant-branches").items.first.id
        s.api.post("/api/invoices", json={"branchId": branch, "lines": [{"qty": 1}]}, expect_status=201)

    listing, post = eks.plan(create).nodes
    assert post.op == "api.post"
    assert post.args == {
        "path": "/api/invoices",
        "expect_status": [201],
        "json": {"branchId": ref_dict(listing, "items", 0, "id"), "lines": [{"qty": 1}]},
    }
    assert "body" not in post.args
    assert post.after == [listing.id]


def test_put_patch_delete_and_request_options(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def requests(s: eks.Session) -> None:
        s.api.put("/api/a", json=[1, 2], headers={"X-Tenant": "harman"}, as_="host")
        s.api.patch("/api/b", json={"name": "x"}, expect_status=(200, 204))
        s.api.delete("/api/c", query={"force": True}, as_="harman-admin", expect_status=[404])
        s.api.post("/api/d")

    put, patch, delete, post = eks.plan(requests).nodes
    assert (put.op, patch.op, delete.op, post.op) == ("api.put", "api.patch", "api.delete", "api.post")
    assert put.args == {"path": "/api/a", "headers": {"X-Tenant": "harman"}, "persona": "host", "json": [1, 2]}
    assert patch.args == {"path": "/api/b", "expect_status": [200, 204], "json": {"name": "x"}}
    assert delete.args == {"path": "/api/c", "query": {"force": True}, "persona": "harman-admin",
                           "expect_status": [404]}
    assert post.args == {"path": "/api/d"}
    # api calls are effectful, so they chain in program order
    assert patch.after == [put.id]
    assert delete.after == [patch.id]
    assert post.after == [delete.id]


@pytest.mark.parametrize(
    ("status", "error", "match"),
    [
        (True, TypeError, "got the bool"),
        ((), ValueError, "is empty"),
        ("200", TypeError, "sequence of them"),
        ((200, "201"), TypeError, "items must be int"),
        ((200, False), TypeError, "items must be int"),
    ],
)
def test_bad_expect_status(eks_registry: Registry, status: Any, error: type[Exception], match: str) -> None:
    @eks.flow(app="web-app", platform="web")
    def bad(s: eks.Session) -> None:
        s.api.get("/api/x", expect_status=status)

    with pytest.raises(error, match=match):
        eks.plan(bad)


def test_as_must_be_a_name(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def bad(s: eks.Session) -> None:
        s.api.get("/api/x", as_=3)  # pyright: ignore[reportArgumentType]

    with pytest.raises(TypeError, match="persona name"):
        eks.plan(bad)


def test_as_must_not_be_empty(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def get(s: eks.Session) -> None:
        s.api.get("/api/x", as_="")

    @eks.flow(app="web-app", platform="web")
    def post(s: eks.Session) -> None:
        s.api.post("/api/x", json={"a": 1}, as_="")

    for flow in (get, post):
        with pytest.raises(ValueError, match="as_ is empty; pass a persona name such as 'harman-admin'"):
            eks.plan(flow)


def test_request_models_refuse_an_empty_persona() -> None:
    # hand-written and MCP plans skip the builder, so the model holds the same line
    with pytest.raises(ValidationError, match="persona"):
        Get.model_validate({"path": "/api/x", "persona": ""})
    with pytest.raises(ValidationError, match="persona"):
        Post.model_validate({"path": "/api/x", "json": {}, "persona": ""})
    assert Get.model_validate({"path": "/api/x", "persona": "host"}).persona == "host"


def test_session_as_uses_lease_and_backend(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", seed="standard")
    def switch(s: eks.Session) -> None:
        s.api.get("/api/me")
        s.as_("harman-admin")
        s.as_("host", at=t("2s"))

    plan = eks.plan(switch)
    rids = resource_ids(plan)
    me, first, second = plan.nodes
    assert first.op == second.op == "session.as"
    assert first.args == {"persona": "harman-admin"}
    assert first.uses == [rids["lease.web"], rids["backend"]]
    assert first.after == [me.id]
    assert first.pin is None
    assert second.args == {"persona": "host"}
    assert second.pin == t("2s")
    assert second.after == [first.id]
    assert '"pin":"2s"' in plan.model_dump_json(by_alias=True, exclude_none=True)


def test_session_as_rejects_bad_personas(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def empty(s: eks.Session) -> None:
        s.as_("")

    @eks.flow(app="web-app", platform="web")
    def number(s: eks.Session) -> None:
        s.as_(7)  # pyright: ignore[reportArgumentType]

    @eks.flow(app="web-app", platform="web")
    def untimed(s: eks.Session) -> None:
        s.as_("host", at="2s")  # pyright: ignore[reportArgumentType]

    with pytest.raises(ValueError, match="persona name"):
        eks.plan(empty)
    with pytest.raises(TypeError, match="persona name"):
        eks.plan(number)
    with pytest.raises(TypeError, match=r"t\('2s'\)"):
        eks.plan(untimed)


def test_backend_info_records_on_every_access(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", seed="standard", backend="eksenapi")
    def info(s: eks.Session) -> dict[str, Any]:
        first = s.backend.info
        second = s.backend.info
        return {"first": first.version, "second": second}

    plan = eks.plan(info)
    rids = resource_ids(plan)
    nodes = ops(plan, "backend.info")
    assert len(nodes) == 2
    first, second = nodes
    assert first.id != second.id
    assert first.args == second.args == {}
    assert first.uses == second.uses == [rids["backend"]]
    assert second.after == [first.id]
    assert first.out == "json"
    assert plan.outputs == {"first": ref_dict(first, "version"), "second": ref_dict(second)}


def test_backend_info_in_a_loop_gets_distinct_ids(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def loop(s: eks.Session) -> None:
        for _ in range(3):
            s.backend.info  # noqa: B018

    nodes = eks.plan(loop).nodes
    assert len({node.id for node in nodes}) == 3
