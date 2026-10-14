import inspect
from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import t
from eks.net import RouteBuilder
from eks.ref import node_of
from eks.tracing import TraceError
from eks_harness.ir.ops.net import Route
from eks_harness.ir.plan import Plan
from eks_harness.ir.presets import NetProfile
from eks_harness.ir.registry import Registry
from eks_harness.ir.time import TimeError


def lease_of(plan: Plan) -> str:
    return next(resource.id for resource in plan.resources if resource.kind.startswith("lease."))


def backend_of(plan: Plan) -> str:
    return next(resource.id for resource in plan.resources if resource.kind == "backend")


def test_design_8_verbatim(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", seed="standard")
    def network(s: eks.Session) -> None:
        s.net.route("GET", "/api/bank-transactions*").delay(t("800ms"))
        s.net.route("POST", "/api/invoices").respond(status=500, json={"error": "x"})
        s.net.route("GET", "/api/rates").patch(lambda_path="items[0].rate", value="2450.10")
        s.net.shape("3g")                                  # deterministic profile
        with s.net.cassette("banka-arama", mode="replay"): # record | replay | auto
            s.api.get("/api/bank-transactions")
        s.inject.script("window.__flags = {beta: true}", when="init")

    plan = eks.plan(network)
    lease = lease_of(plan)
    delay, respond, patch, shape, cassette, script = plan.nodes
    assert [node.op for node in plan.nodes] == [
        "net.route", "net.route", "net.route", "net.shape", "net.cassette", "inject.script"]
    assert delay.args == {
        "method": "GET", "pattern": "/api/bank-transactions*", "action": {"kind": "delay", "by": "0.8s"}}
    assert respond.args == {
        "method": "POST", "pattern": "/api/invoices",
        "action": {"kind": "respond", "status": 500, "json": {"error": "x"}}}
    assert patch.args == {
        "method": "GET", "pattern": "/api/rates",
        "action": {"kind": "patch", "path": "items[0].rate", "value": "2450.10"}}
    assert shape.args == {"profile": "3g"}
    assert cassette.args == {"name": "banka-arama", "mode": "replay"}
    assert script.args == {"source": "window.__flags = {beta: true}", "when": "init"}
    for node in (delay, respond, patch, shape, cassette, script):
        assert node.uses == [lease]
        assert node.out == "none"
    assert cassette.branches is not None
    (inner,) = cassette.branches["body"]
    assert inner.op == "api.get"
    assert inner.uses == [backend_of(plan)]
    # program order: every effectful node chains after the previous one
    assert respond.after == [delay.id]
    assert patch.after == [respond.id]
    assert shape.after == [patch.id]
    assert cassette.after == [shape.id]
    assert inner.after == []
    assert script.after == [cassette.id]
    assert patch.id != delay.id != respond.id


def test_unfinished_route_raises_at_plan_time_with_its_line(eks_registry: Registry) -> None:
    lines: list[int] = []

    @eks.flow(app="web-app", platform="web")
    def dangling(s: eks.Session) -> None:
        frame = inspect.currentframe()
        assert frame is not None
        lines.append(frame.f_lineno + 1)
        s.net.route("GET", "/api/rates")
        s.net.shape("4g")

    with pytest.raises(TraceError, match="has no action") as raised:
        eks.plan(dangling)
    assert f"test_env_net.py:{lines[0]}" in str(raised.value)
    assert "'/api/rates'" in str(raised.value)
    assert ".passthrough()" in str(raised.value)


def test_second_terminal_call_raises(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def twice(s: eks.Session) -> None:
        route = s.net.route("POST", "/api/invoices")
        route.abort()
        route.passthrough()

    with pytest.raises(TraceError, match="already has its action"):
        eks.plan(twice)


def test_every_action_and_route_options(eks_registry: Registry) -> None:
    @eks.flow(app="mobile-app", platform="ios")
    def actions(s: eks.Session) -> None:
        rate = s.api.get("/api/rates").items.first.rate
        s.net.route("*", "/api/a", times=2).abort("timedout")
        s.net.route("DELETE", f"/api/b/{rate}").passthrough()
        s.net.route("PUT", "/api/c").respond(body="plain", headers={"Content-Type": "text/plain"})
        s.net.route("GET", "/api/d").respond()
        s.net.route("GET", "/api/e", at=t("1s")).delay(t("2s"))
        s.net.route("GET", "/api/f").patch(lambda_path="total", value=rate)
        s.net.route("GET", "/api/g").abort()

    plan = eks.plan(actions)
    rates, abort, passthrough, text, empty, pinned, patched, default_abort = plan.nodes
    assert abort.args == {"method": "*", "pattern": "/api/a", "action": {"kind": "abort", "reason": "timedout"},
                          "times": 2}
    assert passthrough.args == {
        "method": "DELETE",
        "pattern": {"$fmt": ["/api/b/", {"$ref": rates.id, "path": ["items", 0, "rate"]}]},
        "action": {"kind": "passthrough"},
    }
    assert rates.id in passthrough.after
    assert text.args["action"] == {
        "kind": "respond", "status": 200, "body": "plain", "headers": {"Content-Type": "text/plain"}}
    assert empty.args["action"] == {"kind": "respond", "status": 200}
    assert pinned.pin == t("1s")
    assert pinned.args["action"] == {"kind": "delay", "by": "2s"}
    assert patched.args["action"] == {
        "kind": "patch", "path": "total", "value": {"$ref": rates.id, "path": ["items", 0, "rate"]}}
    assert default_abort.args["action"] == {"kind": "abort", "reason": "failed"}
    assert all(node.uses == [lease_of(plan)] for node in plan.nodes[1:])


def test_route_builder_is_returned_until_the_action(eks_registry: Registry) -> None:
    seen: list[Any] = []

    @eks.flow(app="web-app", platform="web")
    def staged(s: eks.Session) -> None:
        route = s.net.route("GET", "/api/x")
        seen.append(route)
        seen.append(route.respond(status=204))

    plan = eks.plan(staged)
    assert isinstance(seen[0], RouteBuilder)
    (node,) = plan.nodes
    assert node_of(seen[1]) == node.id


@pytest.mark.parametrize(
    ("build", "error", "match"),
    [
        (lambda s: s.net.route("get", "/x"), ValueError, "upper case"),
        (lambda s: s.net.route("GET", "/x", times=True), TypeError, "times="),
        (lambda s: s.net.route("GET", "/x", times=0).abort(), ValidationError, "times"),
        (lambda s: s.net.route("GET", "/x").delay("800ms"), TypeError, r"t\('800ms'\)"),
        (lambda s: s.net.route("GET", "/x").delay(t.end), TimeError, "net.route delay is a duration"),
        (lambda s: s.net.route("GET", "/x").delay(t("cue:logo")), TimeError, "point in time"),
        (lambda s: s.net.route("GET", "/x").delay(t("-2s")), TimeError, r"delay for GET '/x' at .* positive length"),
        (lambda s: s.net.route("GET", "/x").delay(t("0s")), TimeError, "positive length"),
        (lambda s: s.net.route("GET", "/x").patch(lambda_path="a", value=None), TypeError, "JSON null"),
        (lambda s: s.net.route("GET", "/x").respond(status=700), ValidationError, "status"),
        (lambda s: s.net.route("GET", "/x").respond(status="500"), TypeError, "status="),
        (lambda s: s.net.route("GET", "/x").respond(json={}, body="x"), TypeError, "not both"),
        (lambda s: s.net.route("GET", "/x").patch(lambda_path="", value=1), TypeError, "lambda_path"),
        (lambda s: s.net.route("GET", "/x").abort("bogus"), ValidationError, "reason"),
        (lambda s: s.net.route("GET", "/x", at="1s"), TypeError, "at expects a Time"),
        (lambda s: s.net.shape(3), TypeError, "network preset name"),
        (lambda s: s.net.shape(""), ValueError, "network preset name"),
    ],
)
def test_bad_network_calls(eks_registry: Registry, build: Any, error: type[Exception], match: str) -> None:
    @eks.flow(app="web-app", platform="web")
    def bad(s: eks.Session) -> None:
        route = build(s)
        if isinstance(route, RouteBuilder):
            route.passthrough()

    with pytest.raises(error, match=match):
        eks.plan(bad)


def test_route_args_round_trip_through_the_model(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def routes(s: eks.Session) -> None:
        s.net.route("GET", "/a").delay(t("800ms"))
        s.net.route("POST", "/b").respond(status=500, json={"error": "x"}, headers={"x-a": "1"})
        s.net.route("GET", "/c").patch(lambda_path="items[0].rate", value=0)
        s.net.route("GET", "/d").patch(lambda_path="items[0].flag", value=False)
        s.net.route("GET", "/e").abort("timedout")
        s.net.route("GET", "/f", times=2).passthrough()

    for node in eks.plan(routes).nodes:
        assert Route.model_validate(node.args).model_dump(mode="json", by_alias=True, exclude_none=True) == node.args


def test_patch_model_refuses_null_value() -> None:
    # JSON null would be dropped by the exclude_none dump, so the model refuses it outright
    with pytest.raises(ValidationError, match="cannot be JSON null"):
        Route.model_validate({"method": "GET", "pattern": "/x", "action": {"kind": "patch", "path": "a", "value": None}})


@pytest.mark.parametrize("by", ["-2s", "0s", "cue:logo", "end"])
def test_delay_model_refuses_points_and_non_positive_lengths(by: str) -> None:
    # plans written by hand or over MCP skip the builder, so the model refuses these too
    with pytest.raises(ValidationError, match="duration"):
        Route.model_validate({"method": "GET", "pattern": "/x", "action": {"kind": "delay", "by": by}})


def test_delay_model_accepts_a_positive_length() -> None:
    route = Route.model_validate({"method": "GET", "pattern": "/x", "action": {"kind": "delay", "by": "800ms"}})
    assert route.model_dump(mode="json", by_alias=True, exclude_none=True)["action"] == {"kind": "delay", "by": "0.8s"}


def test_route_finished_outside_its_when_block_raises(eks_registry: Registry) -> None:
    lines: list[int] = []

    @eks.flow(app="web-app", platform="web", seed="standard")
    def escaped(s: eks.Session) -> None:
        info = s.backend.info
        with eks.when(info.flags.beta):
            frame = inspect.currentframe()
            assert frame is not None
            lines.append(frame.f_lineno + 1)
            route = s.net.route("GET", "/x")
        route.respond(status=500)

    with pytest.raises(TraceError, match="finished outside the block it was created in") as raised:
        eks.plan(escaped)
    assert f"test_env_net.py:{lines[0]}" in str(raised.value)
    assert "GET '/x'" in str(raised.value)


def test_route_finished_inside_a_nested_block_raises(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def nested(s: eks.Session) -> None:
        route = s.net.route("GET", "/x")
        with s.net.cassette("kurlar"):
            route.passthrough()

    with pytest.raises(TraceError, match="finished outside the block it was created in"):
        eks.plan(nested)


def test_route_finished_inside_its_own_when_block(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", seed="standard")
    def scoped(s: eks.Session) -> None:
        info = s.backend.info
        with eks.when(info.flags.beta):
            route = s.net.route("GET", "/x")
            route.respond(status=500)

    _, when = eks.plan(scoped).nodes
    assert when.branches is not None
    (route,) = when.branches["then"]
    assert route.op == "net.route"
    assert route.args["action"] == {"kind": "respond", "status": 500}


def test_unroute(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def cleanup(s: eks.Session) -> None:
        s.net.route("GET", "/api/rates").passthrough()
        s.net.unroute("/api/rates")
        s.net.unroute()
        s.net.unroute("/api/*", at=t("3s"))

    plan = eks.plan(cleanup)
    route, one, every, pinned = plan.nodes
    assert (one.op, every.op, pinned.op) == ("net.unroute", "net.unroute", "net.unroute")
    assert one.args == {"pattern": "/api/rates"}
    assert every.args == {}
    assert pinned.pin == t("3s")
    assert one.after == [route.id]
    assert one.uses == [lease_of(plan)]


def test_shape_with_a_profile_model(eks_registry: Registry) -> None:
    profile = NetProfile(latency=t("120ms"), down_kbps=2000, up_kbps=500, loss=0.01)

    @eks.flow(app="web-app", platform="web")
    def shaped(s: eks.Session) -> None:
        s.net.shape(profile, at=t("0.5s"))
        s.net.shape("offline")

    custom, offline = eks.plan(shaped).nodes
    assert custom.args == {
        "profile": {"latency": "0.12s", "down_kbps": 2000, "up_kbps": 500, "loss": 0.01, "offline": False}}
    assert custom.pin == t("0.5s")
    assert offline.args == {"profile": "offline"}


def test_cassette_exports_its_body_and_defaults_to_auto(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def cached(s: eks.Session) -> dict[str, Any]:
        with s.net.cassette("kurlar") as tape:
            rates = s.api.get("/api/rates")
            s.net.route("GET", "/api/rates").passthrough()
        s.api.post("/api/log", json={"rate": rates.items.first})
        return {"tape": tape}

    plan = eks.plan(cached)
    cassette, log = plan.nodes
    assert cassette.args == {"name": "kurlar", "mode": "auto"}
    assert cassette.branches is not None
    rates, route = cassette.branches["body"]
    assert route.after == [rates.id]
    assert log.after == [cassette.id, rates.id]
    assert plan.outputs == {"tape": {"$ref": cassette.id, "path": []}}


def test_cassette_rejects_bad_names_and_modes(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def unnamed(s: eks.Session) -> None:
        with s.net.cassette(""):
            pass

    @eks.flow(app="web-app", platform="web")
    def bad_mode(s: eks.Session) -> None:
        with s.net.cassette("x", mode="rewind"):  # pyright: ignore[reportArgumentType]
            pass

    with pytest.raises(ValidationError):
        eks.plan(unnamed)
    with pytest.raises(ValidationError):
        eks.plan(bad_mode)


def test_route_ids_are_stable_across_plans(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web")
    def stable(s: eks.Session) -> None:
        s.net.route("GET", "/api/a").delay(t("1s"))
        s.net.route("GET", "/api/b").delay(t("1s"))

    first = eks.plan(stable)
    second = eks.plan(stable)
    assert first.to_json() == second.to_json()
    assert first.nodes[0].id != first.nodes[1].id


def test_inject_script_defaults_to_now(eks_registry: Registry) -> None:
    @eks.flow(app="mobile-app", platform="android")
    def scripted(s: eks.Session) -> None:
        s.inject.script("globalThis.__eks = 1")

    (node,) = eks.plan(scripted).nodes
    assert node.op == "inject.script"
    assert node.args == {"source": "globalThis.__eks = 1", "when": "now"}
