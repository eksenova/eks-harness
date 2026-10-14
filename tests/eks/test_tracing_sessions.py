from typing import Any

import pytest

import eks
from eks.tracing import SessionCore, TraceContext, TraceError, current_session, tracing
from eks_harness.ir.hashing import resource_id
from eks_harness.ir.plan import Resource, SessionSpec
from eks_harness.ir.registry import Registry

TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


def make_spec(**fields: Any) -> SessionSpec:
    return SessionSpec.model_validate({"app": "web-app", "platform": "web", **fields})


def test_session_resources_are_stable_identity_hashes(eks_registry: Registry) -> None:
    spec = make_spec(seed="standard", backend="eksenapi")
    ctx = TraceContext(kind="flow", name="resources", registry=eks_registry)
    first = ctx.session_resources(spec)
    again = ctx.session_resources(spec)
    other = TraceContext(kind="flow", name="elsewhere", registry=eks_registry).session_resources(spec)
    assert first == again == other
    assert first["lease"] == resource_id("lease.web", {"app": "web-app", "session": "main"})
    assert first["backend"] == resource_id(
        "backend", {"app": "web-app", "definition": "eksenapi", "seed": "standard", "session": "main"})
    plan = ctx.finalize()
    assert plan.resources == sorted(plan.resources, key=lambda resource: resource.id)
    resources = {resource.id: resource for resource in plan.resources}
    assert resources[first["lease"]] == Resource(id=first["lease"], kind="lease.web", app="web-app", session="main")
    assert resources[first["backend"]] == Resource(
        id=first["backend"], kind="backend", app="web-app", definition="eksenapi", seed="standard", session="main")


def test_none_identity_values_are_left_out(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="none", registry=eks_registry)
    backend = ctx.session_backend(make_spec())
    assert backend == resource_id("backend", {"app": "web-app", "session": "main"})
    (declared,) = ctx.finalize().resources
    assert declared.id == backend
    assert declared.definition is None
    assert declared.seed is None


@pytest.mark.parametrize("fields", [{"seed": "standard"}, {"backend": "eksenapi"}, {"persona": "harman-admin"}])
def test_a_session_that_names_a_seed_backend_or_persona_holds_a_backend(eks_registry: Registry,
                                                                        fields: dict[str, Any]) -> None:
    ctx = TraceContext(kind="flow", name="named", registry=eks_registry)
    assert set(ctx.session_resources(make_spec(**fields))) == {"lease", "backend"}


def test_a_plain_flow_holds_only_its_lease() -> None:
    @eks.flow(app="web-app", platform="web")
    def plain(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)

    plan = eks.plan(plain)
    assert [resource.kind for resource in plan.resources] == ["lease.web"]


def test_an_op_that_uses_the_backend_declares_it_on_first_use() -> None:
    @eks.flow(app="web-app", platform="web")
    def api(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)
        s.record_op("api.get", {"path": "/api/x"})
        s.record_op("api.get", {"path": "/api/y"})

    plan = eks.plan(api)
    kinds = sorted(resource.kind for resource in plan.resources)
    assert kinds == ["backend", "lease.web"]
    backend = next(resource.id for resource in plan.resources if resource.kind == "backend")
    tap, first, second = plan.nodes
    assert backend not in tap.uses and first.uses == second.uses == [backend]


def test_resource_identity_fields_and_params(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="params", registry=eks_registry)
    rid = ctx.resource("cassette", name="banka-arama", mode="replay", session="main", ignored=None)
    assert rid == ctx.resource("cassette", mode="replay", session="main", name="banka-arama")
    assert rid == resource_id("cassette", {"name": "banka-arama", "mode": "replay", "session": "main"})
    assert rid != ctx.resource("cassette", name="banka-arama", mode="record", session="main")
    resources = {resource.id: resource for resource in ctx.finalize().resources}
    assert resources[rid].name == "banka-arama"
    assert resources[rid].session == "main"
    assert resources[rid].params == {"mode": "replay"}


def test_ios_session_uses_an_ios_lease(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="ios", registry=eks_registry)
    spec = SessionSpec(app="mobile-app", platform="ios", key="take-1")
    lease = ctx.session_resources(spec)["lease"]
    assert lease == resource_id("lease.ios", {"app": "mobile-app", "session": "take-1"})


def test_record_op_picks_resources_by_role() -> None:
    captured: dict[str, Any] = {}

    @eks.flow(app="web-app", platform="web", seed="standard")
    def roles(s: eks.Session) -> None:
        captured["resources"] = dict(s.resources)
        s.record_op("api.get", {"path": "/api/x"})
        s.record_op("ui.tap", TAP)
        s.record_op("session.as", {"persona": "harman-admin"})
        s.ctx.record("media.source", {"uri": "file:clip.mp4"})

    plan = eks.plan(roles)
    lease = captured["resources"]["lease"]
    backend = captured["resources"]["backend"]
    api, tap, act, media = plan.nodes
    assert api.uses == [backend]
    assert tap.uses == [lease]
    assert act.uses == [lease, backend]
    assert media.uses == []


def test_session_core_properties(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="props", registry=eks_registry)
    spec = make_spec(seed="standard")
    session = SessionCore(ctx, spec)
    assert session.ctx is ctx
    assert session.spec is spec
    assert session.platform == "web"
    assert session.app == "web-app"
    assert set(session.resources) == {"lease", "backend"}


def test_record_container_uses_the_session(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="containers", registry=eks_registry)
    session = SessionCore(ctx, make_spec())
    with tracing(ctx):
        cond = session.record_op("ui.exists", TAP)
        with session.record_container("ctl.when", {"cond": cond}, branch="then", label="maybe"):
            session.record_op("ui.tap", TAP)
    _exists, when = ctx.finalize().nodes
    assert when.op == "ctl.when"
    assert when.label == "maybe"
    assert when.uses == []
    assert when.branches is not None
    assert when.branches["then"][0].uses == [session.resources["lease"]]


def test_current_session_outside_a_session_raises(eks_registry: Registry) -> None:
    with pytest.raises(TraceError, match="no active session"):
        current_session()
    ctx = TraceContext(kind="edit", name="no-session", registry=eks_registry)
    with tracing(ctx), pytest.raises(TraceError, match="no active session"):
        current_session()


def test_current_session_inside_a_flow_and_nested_activation(eks_registry: Registry) -> None:
    seen: list[SessionCore] = []

    @eks.flow(app="web-app", platform="web")
    def active(s: eks.Session) -> None:
        seen.append(current_session())
        take = SessionCore(s.ctx, s.spec.model_copy(update={"key": "take"}))
        with take.activate():
            seen.append(current_session())
        seen.append(current_session())
        seen.append(s)
        seen.append(take)

    eks.plan(active)
    flow_session, inner, after, root, take = seen
    assert flow_session is root
    assert inner is take
    assert after is root
    with pytest.raises(TraceError):
        current_session()
    assert eks_registry.has_op("session.as")


def test_tracing_isolates_the_session_stack(eks_registry: Registry) -> None:
    outer = TraceContext(kind="flow", name="outer", registry=eks_registry)
    session = SessionCore(outer, make_spec())
    inner = TraceContext(kind="edit", name="inner", registry=eks_registry)
    with tracing(outer), session.activate():
        assert current_session() is session
        with tracing(inner), pytest.raises(TraceError):
            current_session()
        assert current_session() is session


def test_flow_persona_yields_a_leading_session_as_node() -> None:
    @eks.flow(app="web-app", platform="web", seed="standard", as_="harman-admin")
    def persona(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/tenant"})

    plan = eks.plan(persona)
    first, nav = plan.nodes
    assert first.op == "session.as"
    assert first.args == {"persona": "harman-admin"}
    assert plan.session is not None
    lease = resource_id("lease.web", {"app": "web-app", "session": "main"})
    backend = resource_id("backend", {"app": "web-app", "seed": "standard", "session": "main"})
    assert first.uses == [lease, backend]
    assert nav.after == [first.id]
    assert plan.session.persona == "harman-admin"


def test_flow_without_persona_has_no_session_as() -> None:
    @eks.flow(app="web-app", platform="web")
    def anonymous(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/"})

    assert [node.op for node in eks.plan(anonymous).nodes] == ["ui.nav"]


def test_personas_of_a_seed_plan(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="seed", name="personas", registry=eks_registry)
    ctx.add_persona("host")
    ctx.add_persona("harman-admin")
    ctx.add_persona("host")
    assert ctx.finalize().personas == ["host", "harman-admin"]
