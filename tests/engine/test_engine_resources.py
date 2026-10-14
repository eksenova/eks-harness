from __future__ import annotations

import textwrap
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

import eks
from eks_harness import renderq
from eks_harness.daemon.leases import manager
from eks_harness.db.repos import leases as leases_repo
from eks_harness.engine.errors import ResourceError
from eks_harness.engine.model import (
    BackendIdentity,
    BackendRef,
    ResourceRecord,
    RunInfo,
    SeedRestoreResult,
)
from eks_harness.engine.resources import (
    CassetteHooks,
    CassetteNeed,
    CassetteRequest,
    HeldResources,
    ResourcePlan,
    ResourcePlanner,
    ResourceSink,
    SeedHooks,
    SeedRestoreRequest,
    SlotManager,
    cassette_key,
    lease_kinds,
    seed_key,
)
from eks_harness.ir.plan import Node, Plan, Requirement, Resource, SessionSpec
from eks_harness.ir.registry import Registry, contributing, default_registry
from eks_harness.plugins.sdk import BackendChoice, BackendSpec

PROJECT = "acme/res"
NOW = "2026-01-15T09:00+03:00"
TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


@pytest.fixture
def fast_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderq, "POLL_SECONDS", 0.02)


class _Backend:
    def describe(self) -> dict[str, Any]:
        return {}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return ""

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec()

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


class PluginApi(_Backend):
    pass


class PolicyChoosesPolicyApi:
    def choose(self, tree: Path, requested: str | None = None) -> BackendChoice:
        return BackendChoice("policyapi", "the test policy picks it", target="local")


class PolicyAbstains:
    def choose(self, tree: Path, requested: str | None = None) -> BackendChoice:
        return BackendChoice("", "no opinion")


class PolicyChoosesLegacyApi:
    def choose(self, tree: Path, requested: str | None = None) -> BackendChoice:
        return BackendChoice("legacyapi", "the inactive plugin's backend")


class PolicyChoosesRemoteStaging:
    def choose(self, tree: Path, requested: str | None = None) -> BackendChoice:
        return BackendChoice("remote-staging", "a shared deployment", target="https://staging.example.test")


class RecordingSeedHook:
    def __init__(self, label: str = "run") -> None:
        self.label = label
        self.calls: list[tuple[SeedRestoreRequest, dict[str, JsonValue]]] = []

    def restore(self, request: SeedRestoreRequest, info: Mapping[str, JsonValue], *,
                registry: object = None) -> SeedRestoreResult:
        self.calls.append((request, dict(info)))
        return SeedRestoreResult(resource_id=request.resource_id, seed=request.seed, key=request.key, restored=True,
                                 snapshot=f"{self.label}:{request.key[7:15]}")


class RecordingCassetteHook:
    def __init__(self, recorded: set[str] | None = None) -> None:
        self.recorded = set(recorded or ())
        self.asked: list[CassetteRequest] = []

    def available(self, request: CassetteRequest) -> bool:
        self.asked.append(request)
        return request.key in self.recorded

    def opened(self, request: CassetteRequest) -> Mapping[str, JsonValue]:
        return {"mode": request.mode, "key": request.key}

    def closed(self, request: CassetteRequest, recorded: Mapping[str, JsonValue]) -> str | None:
        self.recorded.add(request.key)
        return None


class Sink:
    def __init__(self) -> None:
        self.records: list[ResourceRecord] = []
        self.events: list[tuple[str, dict[str, JsonValue]]] = []

    def sink(self) -> ResourceSink:
        return ResourceSink(record=self.records.append, event=lambda kind, detail: self.events.append(
            (kind, dict(detail))))

    def states(self, resource_id: str) -> list[str]:
        return [record.state for record in self.records if record.resource_id == resource_id]

    def last(self, resource_id: str) -> ResourceRecord:
        return [record for record in self.records if record.resource_id == resource_id][-1]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def make_tree(tmp_path: Path, *, profile: str = "", default: str | None = None, trust: tuple[str, ...] = (),
              disable: tuple[str, ...] = ()) -> Path:
    tree = tmp_path / "repo"
    trusted = ", ".join(f'"{item}"' for item in trust)
    disabled = ", ".join(f'"{item}"' for item in disable)
    project = f'[project]\nid = "{PROJECT}"\n\n[plugins]\ntrust = [{trusted}]\ndisable = [{disabled}]\n'
    if default:
        project += f'\n[backends]\ndefault = "{default}"\n'
    write(tree / ".harness" / "project.toml", project)
    write(tree / "web-app" / ".harness" / "app.toml", '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\n'
          'url = "http://localhost:3000"\ntimezone = "Europe/Istanbul"\nlocale = "tr-TR"\n' + profile)
    return tree


def legacy_backend_plugin(tree: Path, folder: str, plugin_id: str, version: str, backend: str) -> None:
    root = tree / ".harness" / "plugins" / folder
    write(root / "harness-plugin.toml", f"""\
        [plugin]
        id = "{plugin_id}"
        name = "Legacy backend"
        version = "{version}"
        api = "1"

        [[contributes.backend]]
        id = "{backend}"
        entry = "legacy_backend:Backend"
    """)
    write(root / "legacy_backend.py", "class Backend:\n    pass\n")


def legacy_policy_plugin(tree: Path, chooses: str, *, module: str = "acme_legacy_policy",
                         answer: str | None = None) -> None:
    root = tree / ".harness" / "plugins" / "policy"
    write(root / "harness-plugin.toml", f"""\
        [plugin]
        id = "acme.policy"
        name = "Legacy policy"
        version = "4.5.6"
        api = "1"

        [[contributes.backend_policy]]
        id = "branch"
        entry = "{module}:Policy"
    """)
    body = answer or f'return BackendChoice(requested or "{chooses}", "the branch changes the API", target="local")'
    write(root / f"{module}.py", f"""\
        from eks_harness.plugins.sdk import BackendChoice


        class Policy:
            def choose(self, tree, requested=None):
                {body}
    """)


def build(*, backend: str | None = None, seed: str | None = None, ui: bool = False,
          cassette: str | None = None, cassette_mode: str = "replay") -> Plan:
    @eks.flow(app="web-app", platform="web", backend=backend, seed=seed)
    def resources_flow(s: eks.Session) -> None:
        s.record_op("api.get", {"path": "/api/ping"})
        if ui:
            s.record_op("ui.tap", TAP)
        if cassette:
            with s.net.cassette(cassette, mode=cassette_mode):  # pyright: ignore[reportArgumentType]
                s.record_op("ui.tap", TAP)

    return eks.plan(resources_flow)


def standard_seed() -> None:
    @eks.seed("standard", backend="testapi", now=NOW)
    def standard(d: eks.Data) -> None:
        return None


def run_for(plan: Plan, tree: Path | None, run_id: str = "01JRESOURCESRUN0000000000") -> RunInfo:
    return RunInfo(id=run_id, project=PROJECT, session="main", tree=str(tree) if tree else None,
                   plan_hash=plan.hash, plan_kind=plan.kind, plan_name=plan.name, created_at=time.time())


def sessions(plan: Plan) -> dict[str, SessionSpec]:
    return {plan.session.key: plan.session} if plan.session is not None else {}


def planner_for(ctx) -> ResourcePlanner:
    return ResourcePlanner(ctx, seeds=SeedHooks(), cassettes=CassetteHooks())


def backend_need(planner: ResourcePlanner, plan: Plan, registry: Registry, tree: Path | None, **refs: Any):
    needs = planner.plan(plan, registry=registry, tree=str(tree) if tree else None, sessions=sessions(plan), **refs)
    assert len(needs.backends) == 1
    return needs.backends[0]


def caller_lease(ctx, tree: Path, definition: str = "testapi") -> dict[str, Any]:
    return ctx.pools.backends.acquire({"definition": definition, "tree": str(tree), "kind": "agent",
                                       "holder": "caller", "owner": "caller"})


def run_leases(ctx, run_id: str) -> list[dict[str, Any]]:
    return ctx.pools.backends.leases(owner=f"run:{run_id}")


def test_plan_lists_leases_backends_cassettes_and_slots(ctx, registry: Registry, tmp_path: Path) -> None:
    standard_seed()
    planner = planner_for(ctx)
    plan = build(seed="standard", ui=True, cassette="banka", cassette_mode="replay")
    needs = planner.plan(plan, registry=registry, tree=None, sessions=sessions(plan))
    lease = plan.resources[[r.kind for r in plan.resources].index("lease.web")]
    backend = plan.resources[[r.kind for r in plan.resources].index("backend")]
    assert [(n.resource.id, n.platform, n.session_key, n.backend_resource) for n in needs.leases] == [
        (lease.id, "web", "main", backend.id)]
    [need] = needs.backends
    assert need.resource == backend and need.session_key == "main"
    assert need.ref is None and need.source == "plan"
    assert need.seed == "standard"
    from eks.workspace import seed_info

    info = seed_info("standard", registry=registry)
    assert info is not None and need.seed_hash == info.hash
    cassette = next(node for node, _ in plan.walk() if node.op == "net.cassette")
    assert needs.cassettes == [CassetteNeed(node_id=cassette.id, name="banka", mode="replay", session_key="main")]
    assert needs.slots == []
    assert lease_kinds(plan) == ["browser"]

    one = BackendRef(ref="bl_one")
    keyed = BackendRef(ref="testapi@tree:x")
    assert planner.plan(plan, registry=registry, tree=None, sessions=sessions(plan),
                        backend=one).backends[0].ref == one
    chosen = planner.plan(plan, registry=registry, tree=None, sessions=sessions(plan), backend=one,
                          backends={"main": keyed}).backends[0]
    assert chosen.ref == keyed and chosen.source == "request"
    plain = build(ui=True)
    unknown = planner.plan(plain, registry=registry, tree=None, sessions=sessions(plain)).backends[0]
    assert unknown.source is None and unknown.seed is None and unknown.seed_hash is None

    nodes = [Node(id="n0000000001", op="edit.render"), Node(id="n0000000002", op="blender.render"),
             Node(id="n0000000003", op="audio.mix"), Node(id="n0000000004", op="scene.render"),
             Node(id="n0000000005", op="api.get"), Node(id="n0000000006", op="acme.unknown")]
    media = Plan(kind="edit", name="media", nodes=nodes)
    assert planner.plan(media, registry=registry, tree=None, sessions={}).slots == ["node", "blender",
                                                                                    "browser_scene"]
    two = Plan(kind="score", name="two", resources=[
        Resource(id="r0000001", kind="lease.web", app="web-app", session="take0"),
        Resource(id="r0000002", kind="lease.ios", app="mobile-app", session="take1"),
        Resource(id="r0000003", kind="lease.web", app="web-app", session="take2")])
    assert list(lease_kinds(two)) == ["browser", "ios"]
    assert [n.session_key for n in planner.plan(two, registry=registry, tree=None, sessions={}).leases] == [
        "take0", "take1", "take2"]


def test_choose_backend_from_every_source(ctx, client, registry: Registry, tmp_path: Path) -> None:
    planner = planner_for(ctx)
    plan = build(ui=True)
    tree = make_tree(tmp_path)
    caller = caller_lease(ctx, tree)
    lease_id, backend_id = caller["lease"]["id"], caller["backend"]["id"]

    need = backend_need(planner, plan, registry, tree, backend=BackendRef(ref=lease_id))
    choice = planner.choose_backend(need, registry=registry, tree=str(tree), session=plan.session)
    assert (choice.definition, choice.source, choice.ref) == ("testapi", "request", BackendRef(ref=lease_id))
    need = backend_need(planner, plan, registry, tree, backends={"main": BackendRef(ref=backend_id)})
    assert planner.choose_backend(need, registry=registry, tree=str(tree), session=plan.session).definition == \
        "testapi"
    need = backend_need(planner, plan, registry, tree, backend=BackendRef(ref="otherapi"))
    assert planner.choose_backend(need, registry=registry, tree=str(tree), session=plan.session).definition == \
        "otherapi"
    need = backend_need(planner, plan, registry, tree, backend=BackendRef(ref="keyedapi@tree:somewhere"))
    assert planner.choose_backend(need, registry=registry, tree=str(tree), session=plan.session).definition == \
        "keyedapi"
    need = backend_need(planner, plan, registry, tree, backend=BackendRef(ref="bl_0123456789abcdef"))
    with pytest.raises(ResourceError, match="bl_0123456789abcdef"):
        planner.choose_backend(need, registry=registry, tree=str(tree), session=plan.session)

    defined = build(backend="planapi")
    need = backend_need(planner, defined, registry, tree)
    choice = planner.choose_backend(need, registry=registry, tree=str(tree), session=defined.session)
    assert (choice.definition, choice.source) == ("planapi", "plan")

    nothing = make_tree(tmp_path / "empty")
    need = backend_need(planner, plan, registry, nothing)
    with pytest.raises(ResourceError) as missing:
        planner.choose_backend(need, registry=registry, tree=str(nothing), session=plan.session)
    assert missing.value.message == ("no backend for session main: name one with backend= on the flow or its seed, "
                                     "pass --backend, or add a backend policy")
    with pytest.raises(ResourceError, match="no backend for session main"):
        planner.choose_backend(need, registry=registry, tree=None, session=plan.session)

    defaulted = make_tree(tmp_path / "defaulted", default="defaultapi")
    choice = planner.choose_backend(need, registry=registry, tree=str(defaulted), session=plan.session)
    assert (choice.definition, choice.source) == ("defaultapi", "project_default")

    legacy = make_tree(tmp_path / "legacy", default="defaultapi", trust=("policy",))
    legacy_policy_plugin(legacy, "legacyapi")
    eks.backend("legacyapi")(PluginApi)
    eks.backend("policyapi")(PluginApi)
    choice = planner.choose_backend(need, registry=registry, tree=str(legacy), session=plan.session)
    assert (choice.definition, choice.source, choice.policy, choice.policy_plugin, choice.policy_version) == (
        "legacyapi", "legacy_policy", "acme.policy:backend_policy:branch", "acme.policy", "4.5.6")

    eks.backend_policy("a_abstains")(PolicyAbstains)
    with contributing("acme.policies", "2.0.0"):
        eks.backend_policy("b_policy")(PolicyChoosesPolicyApi)
    choice = planner.choose_backend(need, registry=registry, tree=str(legacy), session=plan.session)
    assert (choice.definition, choice.source, choice.policy, choice.policy_plugin, choice.policy_version) == (
        "policyapi", "policy", "b_policy", "acme.policies", "2.0.0")

    profiled = make_tree(tmp_path / "profiled", default="defaultapi",
                         profile='\n[backend]\ndefinition = "profileapi"\ntree = "../api"\nkey = "k1"\n'
                                 '[backend.params]\nport = 1\n')
    choice = planner.choose_backend(need, registry=registry, tree=str(profiled), session=plan.session)
    assert (choice.definition, choice.source) == ("profileapi", "profile")
    assert choice.ref == BackendRef(ref="profileapi", tree=str((profiled / "api").resolve()), key="k1",
                                    params={"port": "1"})
    linked = make_tree(tmp_path / "linked", profile=f'\n[backend]\nlink = "{lease_id}"\n')
    choice = planner.choose_backend(need, registry=registry, tree=str(linked), session=plan.session)
    assert (choice.definition, choice.source) == ("testapi", "profile")
    off = make_tree(tmp_path / "off", default="defaultapi", profile='\n[backend]\ndefinition = "none"\n')
    with pytest.raises(ResourceError) as turned_off:
        planner.choose_backend(need, registry=registry, tree=str(off), session=plan.session)
    assert turned_off.value.message == ("the app profile of web-app turns the backend off ([backend] definition = "
                                        "none); pass --backend or remove the setting")
    unlinked = make_tree(tmp_path / "unlinked", default="defaultapi", profile='\n[backend]\nlink = "Off"\n')
    with pytest.raises(ResourceError, match=r"\(\[backend\] link = Off\)"):
        planner.choose_backend(need, registry=registry, tree=str(unlinked), session=plan.session)
    requested = backend_need(planner, plan, registry, off, backend=BackendRef(ref="otherapi"))
    assert planner.choose_backend(requested, registry=registry, tree=str(off), session=plan.session).source == \
        "request"


def test_policy_decisions_must_name_backend_definitions(ctx, client, registry: Registry, tmp_path: Path) -> None:
    planner = planner_for(ctx)
    plan = build(ui=True)
    staging = make_tree(tmp_path / "staging", default="eksenapi", trust=("policy",))
    legacy_policy_plugin(staging, "", module="acme_staging_policy",
                         answer='return BackendChoice("staging", "the branch does not change the API", '
                                'target="https://api-staging.example.test")')
    need = backend_need(planner, plan, registry, staging)
    with pytest.raises(ResourceError) as refused:
        planner.choose_backend(need, registry=registry, tree=str(staging), session=plan.session)
    assert refused.value.message == (
        "backend policy acme.policy:backend_policy:branch decided staging (target https://api-staging.example.test: "
        f"the branch does not change the API), which is not a backend definition of {staging}; the engine only "
        "leases managed backends: pass --backend <definition>, or name one with backend= on the flow or its seed")
    run = run_for(plan, staging)
    needs = planner.plan(plan, registry=registry, tree=str(staging), sessions=sessions(plan))
    recorder = Sink()
    with pytest.raises(ResourceError, match="decided staging"):
        planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                        cancel=threading.Event())
    assert recorder.states(needs.backends[0].resource.id) == ["pending", "failed"]
    assert run_leases(ctx, run.id) == []
    eks.backend("staging")(PluginApi)
    choice = planner.choose_backend(need, registry=registry, tree=str(staging), session=plan.session)
    assert (choice.definition, choice.source) == ("staging", "legacy_policy")

    failing = make_tree(tmp_path / "failing", default="eksenapi", trust=("policy",))
    legacy_policy_plugin(failing, "", module="acme_failing_policy",
                         answer='raise RuntimeError("git is not installed")')
    failing_need = backend_need(planner, plan, registry, failing)
    with pytest.raises(ResourceError) as broken:
        planner.choose_backend(failing_need, registry=registry, tree=str(failing), session=plan.session)
    assert broken.value.message == "backend policy acme.policy:backend_policy:branch failed: git is not installed"

    eks.backend_policy("remote")(PolicyChoosesRemoteStaging)
    with pytest.raises(ResourceError) as remote:
        planner.choose_backend(need, registry=registry, tree=str(staging), session=plan.session)
    assert remote.value.message.startswith(
        "backend policy remote decided remote-staging (target https://staging.example.test: a shared deployment), "
        "which is not a backend definition of")


def test_acquire_takes_a_run_owned_lease_on_the_callers_instance(ctx, client, registry: Registry,
                                                                 tmp_path: Path) -> None:
    with contributing("acme.api", "1.2.0"):
        eks.backend("testapi")(PluginApi)
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build()
    caller = caller_lease(ctx, tree)
    caller_id, backend_id = caller["lease"]["id"], caller["backend"]["id"]
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan),
                         backend=BackendRef(ref=caller_id))
    recorder = Sink()
    held = planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                           cancel=threading.Event(), seed_mode="skip")
    [resource_id] = list(held.resources.backends)
    handle = planner.backend_handle(held, resource_id)
    assert handle is not None and handle is held.resources.backend(resource_id)
    assert planner.backend_handle(held, "rmissing") is None and planner.lease_handle(held, resource_id) is None
    owned = run_leases(ctx, run.id)
    assert sorted(item["kind"] for item in owned) == ["agent", "driver"]
    mine = next(item for item in owned if item["kind"] == "agent")
    [lease_handle] = held.resources.leases.values()
    assert next(item for item in owned if item["kind"] == "driver")["driverSid"] == lease_handle.sid
    assert (mine["kind"], mine["holder"], mine["owner"], mine["backendId"]) == (
        "agent", f"run {run.id}", f"run:{run.id}", backend_id)
    assert (mine["project"], mine["session"]) == (PROJECT, "main")
    assert mine["id"] != caller_id and held.backend_leases == {resource_id: mine["id"]}
    identity = handle.identity
    assert identity.model_dump(by_alias=False) == {
        "definition": "testapi", "backend_id": backend_id, "lease_id": mine["id"], "source": "request",
        "policy": None, "policy_plugin": None, "policy_version": None, "plugin": "acme.api",
        "plugin_version": "1.2.0", "fingerprint": "fake", "commit": "0" * 40, "branch": "main", "version": "fake",
        "generation": 1}
    assert handle.url == ctx.pools.backends.info(backend_id)["url"] and handle.url.startswith("http://127.0.0.1:")
    assert handle.info["credentials"] != handle.secrets["credentials"]
    assert handle.secrets["credentials"] == {"admin": {"username": "admin", "password": "fake-testapi-secret"}}
    assert "fake-testapi-secret" not in str(handle.info)
    dumped = handle.model_dump()
    assert "secrets" not in dumped and "fake-testapi-secret" not in handle.model_dump_json()
    assert "fake-testapi-secret" not in repr(handle) and "fake-testapi-secret" not in repr(held.resources)
    assert recorder.states(resource_id) == ["pending", "acquiring", "held"]
    final = recorder.last(resource_id)
    assert final.kind == "backend" and final.handle == mine["id"] and final.acquired_at is not None
    assert final.detail["plugin"] == "acme.api" and final.detail["backendId"] == backend_id
    assert "fake-testapi-secret" not in str(final.model_dump())

    planner.release(held, "run finished", recorder.sink())
    assert run_leases(ctx, run.id) == []
    assert ctx.pools.backends.find_lease(caller_id) is not None
    assert recorder.last(resource_id).state == "released"
    assert recorder.last(resource_id).released_at is not None


def test_acquire_restores_seeds_through_the_run_hook(ctx, client, registry: Registry, tmp_path: Path) -> None:
    standard_seed()
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(seed="standard")
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan))
    registered = RecordingSeedHook("registered")
    planner.seeds.register("testapi", registered)
    hook = RecordingSeedHook()
    recorder = Sink()
    held = planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                           cancel=threading.Event(), seed_hook=hook)
    [need] = needs.backends
    handle = held.resources.backends[need.resource.id]
    assert handle.identity.source == "plan" and handle.seed == "standard"
    [(request, info)] = hook.calls
    assert registered.calls == []
    assert request.key == seed_key(need.seed_hash, handle.identity)
    assert (request.seed, request.seed_hash, request.resource_id, request.session_key) == (
        "standard", need.seed_hash, need.resource.id, "main")
    assert request.backend == handle.identity and request.url == handle.url and request.run == run
    assert request.now == "2026-01-15T09:00:00+03:00"
    assert info["credentials"] == {"admin": {"username": "admin", "password": "fake-testapi-secret"}}
    [restored] = held.resources.seeds
    assert restored.restored and restored.key == request.key
    seed_id = f"seed:{need.resource.id}"
    assert recorder.states(seed_id) == ["pending", "acquiring", "held"]
    assert recorder.last(seed_id).kind == "seed" and recorder.last(seed_id).handle == request.key
    planner.release(held, "done", recorder.sink())
    assert recorder.last(seed_id).state == "released"

    second = run_for(plan, tree, "01JRESOURCESRUN0000000002")
    held = planner.acquire(second, plan, needs, registry=registry, sessions=sessions(plan), sink=Sink().sink(),
                           cancel=threading.Event())
    assert len(registered.calls) == 1 and held.resources.seeds[0].snapshot is not None
    planner.release(held, "done", Sink().sink())

    skipped = Sink()
    third = run_for(plan, tree, "01JRESOURCESRUN0000000003")
    bare = planner_for(ctx)
    held = bare.acquire(third, plan, needs, registry=registry, sessions=sessions(plan), sink=skipped.sink(),
                        cancel=threading.Event(), seed_mode="skip")
    assert held.resources.seeds == [] and skipped.states(seed_id) == []
    bare.release(held, "done", skipped.sink())

    failing = Sink()
    fourth = run_for(plan, tree, "01JRESOURCESRUN0000000004")
    with pytest.raises(ResourceError) as missing:
        bare.acquire(fourth, plan, needs, registry=registry, sessions=sessions(plan), sink=failing.sink(),
                     cancel=threading.Event())
    assert missing.value.message.startswith("no seed engine can restore the seed standard into the backend testapi")
    assert failing.states(seed_id) == ["pending", "failed"]
    assert failing.last(need.resource.id).state == "released"
    assert run_leases(ctx, fourth.id) == []

    unregistered = needs.model_copy(update={"backends": [need.model_copy(update={"seed": "ghost",
                                                                                "seed_hash": None})]})
    hook = RecordingSeedHook()
    fifth = run_for(plan, tree, "01JRESOURCESRUN0000000005")
    lost = Sink()
    with pytest.raises(ResourceError) as unknown:
        planner.acquire(fifth, plan, unregistered, registry=registry, sessions=sessions(plan), sink=lost.sink(),
                        cancel=threading.Event(), seed_hook=hook)
    assert unknown.value.message == (f"seed ghost is not registered in {tree}; register it with @eks.seed or run with "
                                     "seed=skip")
    assert hook.calls == [] and lost.states(seed_id) == ["pending", "failed"]
    assert run_leases(ctx, fifth.id) == []


def test_acquire_takes_a_browser_lease_linked_to_the_backend(ctx, client, registry: Registry,
                                                             tmp_path: Path) -> None:
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(backend="testapi", ui=True)
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan))
    recorder = Sink()
    held = planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                           cancel=threading.Event())
    [lease_need] = needs.leases
    handle = planner.lease_handle(held, lease_need.resource.id)
    assert handle is not None
    assert (handle.kind, handle.platform, handle.session_key, handle.app, handle.project, handle.session) == (
        "lease.web", "web", "main", "web-app", PROJECT, "main")
    assert handle.resource is not None and handle.resource.startswith("browser:")
    assert held.driver_sids == {lease_need.resource.id: handle.sid}
    lease = leases_repo.get_by_sid(ctx.db.conn(), handle.sid)
    assert lease is not None and lease.state == "active" and lease.phase == "ready"
    assert lease.owner_instance == f"run:{run.id}" and lease.label == f"run {run.id}"
    assert lease.meta["tags"] == [f"run:{run.id}"]
    assert lease.meta["claim"]["run"] == run.id and lease.meta["claim"]["host"]
    assert lease.tree == str(tree)
    backend_handle = held.resources.backends[lease_need.backend_resource or ""]
    linked = ctx.pools.backends.link_of(handle.sid)
    assert linked is not None and linked[0]["id"] == backend_handle.identity.backend_id
    assert recorder.states(lease_need.resource.id) == ["pending", "acquiring", "held"]
    assert recorder.last(lease_need.resource.id).handle == handle.sid

    planner.release(held, "run finished", recorder.sink())
    released = leases_repo.get_by_sid(ctx.db.conn(), handle.sid)
    assert released is not None and released.state == "released"
    assert ctx.pools.backends.link_of(handle.sid) is None
    assert run_leases(ctx, run.id) == []
    assert recorder.last(lease_need.resource.id).state == "released"


def test_two_leases_of_one_kind_get_their_own_devices(ctx, client, registry: Registry, tmp_path: Path) -> None:
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = Plan(kind="score", name="two", resources=[
        Resource(id="r0000001", kind="lease.web", app="web-app", session="take0"),
        Resource(id="r0000002", kind="lease.web", app="web-app", session="take1")])
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions={})
    held = planner.acquire(run, plan, needs, registry=registry, sessions={}, sink=Sink().sink(),
                           cancel=threading.Event())
    first, second = held.resources.leases["r0000001"], held.resources.leases["r0000002"]
    assert first.sid != second.sid and first.resource != second.resource
    instances = {leases_repo.get_by_sid(ctx.db.conn(), sid).owner_instance  # pyright: ignore[reportOptionalMemberAccess]
                 for sid in (first.sid, second.sid)}
    assert instances == {f"run:{run.id}", f"run:{run.id}/r0000002"}
    planner.release(held, "done", Sink().sink())


def test_a_queued_run_lease_keeps_polling_past_the_queue_timeout(ctx, client, registry: Registry,
                                                                 tmp_path: Path) -> None:
    ctx.config.set_many({"browser.instances": 1, "browser.profilesPerInstance": 1, "lease.queueTimeoutSeconds": 5})
    leases = manager(ctx)
    blocker = leases.acquire({"kind": "browser", "project": PROJECT, "session": "main", "instance": "blocker"},
                             "blocker", wait=True, wait_seconds=10)
    assert blocker["status"] == "granted"
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(backend="testapi", ui=True)
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan))
    [lease_need] = needs.leases
    recorder = Sink()
    outcome: dict[str, Any] = {}

    def acquire() -> None:
        try:
            outcome["held"] = planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan),
                                              sink=recorder.sink(), cancel=threading.Event())
        except ResourceError as error:
            outcome["error"] = error

    worker = threading.Thread(target=acquire, daemon=True)
    worker.start()
    expired: list[str] = []
    deadline = time.time() + 7.5
    while time.time() < deadline and worker.is_alive():
        expired += [lease.sid for lease in leases.expire_queue()]
        time.sleep(0.5)
    assert worker.is_alive() and expired == [] and "error" not in outcome
    waiting = [record for record in recorder.records
               if record.resource_id == lease_need.resource.id and record.detail.get("status") == "queued"]
    assert waiting and waiting[-1].detail["position"] == 1
    queued = leases_repo.get_by_sid(ctx.db.conn(), str(waiting[-1].detail["sid"]))
    assert queued is not None and queued.state == "queued"
    assert queued.last_poll_at is not None and time.time() - queued.last_poll_at < 5

    leases.release(blocker["sid"], "the blocker is done", actor="blocker")
    worker.join(30)
    assert not worker.is_alive() and "error" not in outcome, outcome.get("error")
    held: HeldResources = outcome["held"]
    handle = planner.lease_handle(held, lease_need.resource.id)
    assert handle is not None and handle.sid == waiting[-1].detail["sid"]
    granted = leases_repo.get_by_sid(ctx.db.conn(), handle.sid)
    assert granted is not None and granted.state == "active" and granted.resource == handle.resource
    assert recorder.last(lease_need.resource.id).state == "held"
    planner.release(held, "done", recorder.sink())


def test_replay_cassettes_must_exist(ctx, client, registry: Registry, tmp_path: Path) -> None:
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(backend="testapi", ui=True, cassette="banka")
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan))
    [cassette] = needs.cassettes
    recorder = Sink()
    with pytest.raises(ResourceError) as missing:
        planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                        cancel=threading.Event())
    assert "cassette_missing" in missing.value.message and missing.value.detail["code"] == "cassette_missing"
    cassette_id = f"cassette:{cassette.node_id}"
    assert recorder.states(cassette_id) == ["pending", "acquiring", "failed"]
    [lease_need] = needs.leases
    assert recorder.last(lease_need.resource.id).state == "released"
    sid = recorder.last(lease_need.resource.id).handle or ""
    gone = leases_repo.get_by_sid(ctx.db.conn(), sid)
    assert gone is not None and gone.state == "released"
    assert run_leases(ctx, run.id) == []

    hook = RecordingCassetteHook()
    second = run_for(plan, tree, "01JRESOURCESRUN0000000002")
    with pytest.raises(ResourceError, match="cassette_missing"):
        planner.acquire(second, plan, needs, registry=registry, sessions=sessions(plan), sink=Sink().sink(),
                        cancel=threading.Event(), cassette_hook=hook)
    asked = hook.asked[-1]
    assert asked.backend is not None and asked.mode == "replay" and asked.name == "banka"
    node = plan.node(cassette.node_id)
    assert asked.key == cassette_key(plan, node, asked.backend.model_copy(update={"lease_id": "other"}))
    hook.recorded.add(asked.key)
    third = run_for(plan, tree, "01JRESOURCESRUN0000000003")
    recorder = Sink()
    held = planner.acquire(third, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                           cancel=threading.Event(), cassette_hook=hook)
    assert recorder.states(cassette_id) == ["pending", "acquiring", "held"]
    assert recorder.last(cassette_id).handle == asked.key
    planner.release(held, "done", recorder.sink())

    planner.cassettes.register(RecordingCassetteHook())
    planner.cassettes.register(hook)
    assert planner.cassettes.hook() is hook
    assert planner.cassette_available(third, plan, cassette, asked.backend)
    assert not planner.cassette_available(third, plan, cassette, None)
    assert not planner_for(ctx).cassette_available(third, plan, cassette, asked.backend)
    recording = build(backend="testapi", ui=True, cassette="banka", cassette_mode="record")
    fourth = run_for(recording, tree, "01JRESOURCESRUN0000000004")
    record_needs = planner_for(ctx).plan(recording, registry=registry, tree=str(tree), sessions=sessions(recording))
    held = planner_for(ctx).acquire(fourth, recording, record_needs, registry=registry,
                                    sessions=sessions(recording), sink=Sink().sink(), cancel=threading.Event())
    assert held.resources.leases
    planner_for(ctx).release(held, "done", Sink().sink())


def test_a_cancelled_acquisition_releases_everything(ctx, client, registry: Registry, tmp_path: Path) -> None:
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(backend="testapi", ui=True)
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan))
    cancel = threading.Event()

    class CancellingSink(Sink):
        def sink(self) -> ResourceSink:
            def record(item: ResourceRecord) -> None:
                self.records.append(item)
                if item.kind == "backend" and item.state == "held":
                    cancel.set()

            return ResourceSink(record=record, event=lambda kind, detail: None)

    recorder = CancellingSink()
    with pytest.raises(ResourceError, match="cancelled"):
        planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                        cancel=cancel)
    [backend] = needs.backends
    assert recorder.last(backend.resource.id).state == "released"
    assert run_leases(ctx, run.id) == []
    assert [record for record in recorder.records if record.kind == "lease.web"] == []


def test_plugin_requirements_and_inactive_plugins_fail(ctx, client, registry: Registry, tmp_path: Path) -> None:
    with contributing("acme.api", "1.2.0"):
        eks.backend("testapi")(PluginApi)
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(backend="testapi")
    stale = plan.model_copy(update={"requires": [Requirement(plugin="acme.api", version="1.0.0")]})
    run = run_for(stale, tree)
    needs = planner.plan(stale, registry=registry, tree=str(tree), sessions=sessions(stale))
    recorder = Sink()
    with pytest.raises(ResourceError) as mismatch:
        planner.acquire(run, stale, needs, registry=registry, sessions=sessions(stale), sink=recorder.sink(),
                        cancel=threading.Event())
    assert "built with plugin acme.api 1.0.0" in mismatch.value.message
    assert "comes from acme.api 1.2.0" in mismatch.value.message
    assert recorder.states(needs.backends[0].resource.id) == ["pending", "failed"]
    assert run_leases(ctx, run.id) == []
    current = plan.model_copy(update={"requires": [Requirement(plugin="acme.api", version="1.2.0")]})
    held = planner.acquire(run_for(current, tree, "01JRESOURCESRUN0000000002"), current, needs, registry=registry,
                           sessions=sessions(current), sink=Sink().sink(), cancel=threading.Event())
    planner.release(held, "done", Sink().sink())

    inactive = make_tree(tmp_path / "inactive", trust=("legacy",), disable=("acme.legacy",))
    legacy_backend_plugin(inactive, "legacy", "acme.legacy", "3.1.0", "legacyapi")
    eks.backend_policy("chooser")(PolicyChoosesLegacyApi)
    unplanned = build()
    run = run_for(unplanned, inactive, "01JRESOURCESRUN0000000003")
    needs = planner.plan(unplanned, registry=registry, tree=str(inactive), sessions=sessions(unplanned))
    with pytest.raises(ResourceError) as disabled:
        planner.acquire(run, unplanned, needs, registry=registry, sessions=sessions(unplanned), sink=Sink().sink(),
                        cancel=threading.Event())
    assert disabled.value.message == (
        "backend policy chooser chose backend legacyapi, but its plugin acme.legacy is not active in "
        f"{inactive}; trust and enable the plugin or choose another backend")
    assert run_leases(ctx, run.id) == []

    active = make_tree(tmp_path / "active", trust=("legacy",))
    legacy_backend_plugin(active, "legacy", "acme.legacy", "3.1.0", "legacyapi")
    run = run_for(unplanned, active, "01JRESOURCESRUN0000000004")
    needs = planner.plan(unplanned, registry=registry, tree=str(active), sessions=sessions(unplanned))
    held = planner.acquire(run, unplanned, needs, registry=registry, sessions=sessions(unplanned),
                           sink=Sink().sink(), cancel=threading.Event())
    [handle] = held.resources.backends.values()
    assert (handle.identity.definition, handle.identity.source, handle.identity.policy) == (
        "legacyapi", "policy", "chooser")
    assert (handle.identity.plugin, handle.identity.plugin_version) == ("acme.legacy", "3.1.0")
    planner.release(held, "done", Sink().sink())


def identity(**changes: Any) -> BackendIdentity:
    base = BackendIdentity(definition="testapi", backend_id="testapi@tree:x", lease_id="bl_1", source="plan",
                           plugin="acme.api", plugin_version="1.0.0", fingerprint="f1", commit="c1", branch="main",
                           version="v1", generation=1)
    return base.model_copy(update=changes)


def test_seed_and_cassette_keys_follow_the_backend_identity(registry: Registry) -> None:
    base = identity()
    key = seed_key("sha256:" + "1" * 64, base)
    assert key.startswith("sha256:") and len(key) == 71
    for field in ("definition", "plugin_version", "fingerprint", "version", "commit", "plugin"):
        assert seed_key("sha256:" + "1" * 64, identity(**{field: "changed"})) != key, field
    for field, value in (("lease_id", "bl_2"), ("backend_id", "testapi@tree:y"), ("generation", 7),
                         ("branch", "other"), ("source", "request"), ("policy", "p")):
        assert seed_key("sha256:" + "1" * 64, identity(**{field: value})) == key, field
    assert seed_key("sha256:" + "2" * 64, base) != key and seed_key(None, base) != key

    plan = build(backend="testapi", ui=True, cassette="banka")
    node = next(node for node, _ in plan.walk() if node.op == "net.cassette")
    ckey = cassette_key(plan, node, base)
    for field in ("definition", "fingerprint", "version", "commit"):
        assert cassette_key(plan, node, identity(**{field: "changed"})) != ckey, field
    for field, value in (("lease_id", "bl_2"), ("generation", 3), ("branch", "dev")):
        assert cassette_key(plan, node, identity(**{field: value})) == ckey, field
    assert cassette_key(plan, node, None) != ckey
    other = build(backend="testapi", ui=True, cassette="kasa")
    other_node = next(node for node, _ in other.walk() if node.op == "net.cassette")
    assert cassette_key(other, other_node, base) != ckey


def test_keepalive_heartbeats_renews_and_reports_lost_resources(ctx, client, registry: Registry, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(backend="testapi", ui=True)
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan))
    recorder = Sink()
    held = planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                           cancel=threading.Event())
    [lease_need], [backend] = needs.leases, needs.backends
    sid = held.resources.leases[lease_need.resource.id].sid
    lease_id = held.backend_leases[backend.resource.id]
    _record, backend_lease = ctx.pools.backends.require_lease(lease_id)
    backend_lease["expires"] = time.time() + 5
    driver = leases_repo.get_by_sid(ctx.db.conn(), sid)
    assert driver is not None
    with ctx.db.transaction() as conn:
        leases_repo.update(conn, driver.id, heartbeat_at=time.time() - 100)
    leases = manager(ctx)
    leases.last_activity = 0.0
    planner.keepalive(held)
    renewed = ctx.pools.backends.require_lease(lease_id)[1]
    assert renewed["expires"] > time.time() + 1700 and renewed["ttl"] == 1800
    beaten = leases_repo.get_by_sid(ctx.db.conn(), sid)
    assert beaten is not None and beaten.heartbeat_at is not None and beaten.heartbeat_at > time.time() - 10
    assert leases.last_activity > time.time() - 10
    assert recorder.events == []

    def broken_heartbeat(sid: str, meta: dict[str, Any] | None = None) -> Any:
        raise RuntimeError("database is locked")

    ctx.pools.backends.require_lease(lease_id)[1]["expires"] = time.time() + 5
    with monkeypatch.context() as patched:
        patched.setattr(leases, "heartbeat", broken_heartbeat)
        planner.keepalive(held)
    assert ctx.pools.backends.require_lease(lease_id)[1]["expires"] > time.time() + 1700
    assert recorder.events == [] and planner.lease_handle(held, lease_need.resource.id) is not None

    leases.release(sid, "broken by the test")
    ctx.pools.backends.release(lease_id, "dropped by the test")
    planner.keepalive(held)
    lost = [detail for kind, detail in recorder.events if kind == "resource.lost"]
    assert sorted((item["resource"], item["kind"]) for item in lost) == sorted(
        [(lease_need.resource.id, "lease"), (backend.resource.id, "backend")])
    assert planner.lease_handle(held, lease_need.resource.id) is None
    assert planner.backend_handle(held, backend.resource.id) is None
    planner.keepalive(held)
    assert len([kind for kind, _ in recorder.events if kind == "resource.lost"]) == 2
    planner.release(held, "done", recorder.sink())
    assert recorder.last(lease_need.resource.id).state == "released"


def test_release_order_and_errors_never_raise(ctx, client, registry: Registry, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    planner = planner_for(ctx)
    tree = make_tree(tmp_path)
    plan = build(backend="testapi", ui=True)
    run = run_for(plan, tree)
    needs = planner.plan(plan, registry=registry, tree=str(tree), sessions=sessions(plan))
    recorder = Sink()
    held = planner.acquire(run, plan, needs, registry=registry, sessions=sessions(plan), sink=recorder.sink(),
                           cancel=threading.Event())
    calls: list[str] = []
    leases = manager(ctx)
    real_backend_release = ctx.pools.backends.release

    def failing_release(sid: str, reason: str | None = None, actor: str | None = None) -> list[Any]:
        calls.append(f"driver {sid} {reason} {actor}")
        raise RuntimeError("the database is locked")

    def backend_release(lease_id: str, reason: str = "released") -> dict[str, Any] | None:
        calls.append(f"backend {lease_id} {reason}")
        return real_backend_release(lease_id, reason)

    monkeypatch.setattr(leases, "release", failing_release)
    monkeypatch.setattr(ctx.pools.backends, "release", backend_release)
    planner.release(held, "run finished", recorder.sink())
    [lease_need], [backend] = needs.leases, needs.backends
    sid = recorder.last(lease_need.resource.id).handle
    assert calls == [f"driver {sid} run finished run:{run.id}",
                     f"backend {recorder.last(backend.resource.id).handle} run finished"]
    assert recorder.last(lease_need.resource.id).state == "released"
    assert "the database is locked" in (recorder.last(lease_need.resource.id).error or "")
    assert recorder.last(backend.resource.id).state == "released"
    assert recorder.last(backend.resource.id).error is None
    assert [item["kind"] for item in run_leases(ctx, run.id)] == ["driver"]
    monkeypatch.undo()
    manager(ctx).release(sid or "", "cleanup")
    assert run_leases(ctx, run.id) == []
    empty = HeldResources(resources=held.resources.model_copy(update={"leases": {}, "backends": {}}))
    planner.release(empty, "nothing held", recorder.sink())


def test_seed_hooks_pick_exact_then_wildcard() -> None:
    hooks = SeedHooks()
    assert hooks.hook("testapi") is None
    anywhere, exact, later = RecordingSeedHook("any"), RecordingSeedHook("exact"), RecordingSeedHook("later")
    hooks.register("*", anywhere)
    assert hooks.hook("testapi") is anywhere
    hooks.register("testapi", exact)
    assert hooks.hook("testapi") is exact and hooks.hook("other") is anywhere
    hooks.register("testapi", later)
    assert hooks.hook("testapi") is later
    with pytest.raises(ValueError):
        hooks.register("", exact)
    assert CassetteHooks().hook() is None
    assert ResourcePlan().slots == []


def test_slot_manager_uses_the_render_queue(paths, fast_queue) -> None:
    slots = SlotManager()
    slot = slots.acquire("node", "edit.render n1", run_id="01RUNSLOT", cancel=threading.Event())
    assert slot.location == "node" and slot.label == "edit.render n1" and slot.acquired_at > 0
    [item] = renderq.status()["items"]
    assert (item["id"], item["kind"], item["session"], item["state"]) == (slot.ticket, "node", "01RUNSLOT",
                                                                          "running")
    cancel = threading.Event()
    positions: list[tuple[int, str]] = []
    outcome: dict[str, BaseException] = {}

    def waiter() -> None:
        try:
            slots.acquire("blender", "blender.render n2", run_id="01RUNSLOT", cancel=cancel,
                          on_wait=lambda position, ticket: positions.append((position, ticket)))
        except RuntimeError as error:
            outcome["error"] = error

    thread = threading.Thread(target=waiter)
    thread.start()
    deadline = time.time() + 5
    while not positions and time.time() < deadline:
        time.sleep(0.01)
    assert positions and positions[0][0] == 1
    waiting_ticket = positions[0][1]
    assert any(item["id"] == waiting_ticket for item in renderq.status()["items"])
    cancel.set()
    thread.join(5)
    assert str(outcome["error"]) == "cancelled"
    assert [item["id"] for item in renderq.status()["items"]] == [slot.ticket]
    slots.release(slot)
    assert renderq.status()["items"] == []
    again = slots.acquire("browser_scene", "scene.render n3", run_id="01RUNSLOT", cancel=threading.Event())
    slots.release(again)
    slots.release(again)
    assert renderq.status()["items"] == []
