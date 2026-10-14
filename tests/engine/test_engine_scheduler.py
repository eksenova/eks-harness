from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

import eks
from eks_harness.config import Config
from eks_harness.daemon.context import AppContext
from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import projects, sessions
from eks_harness.engine import scheduler as scheduler_module
from eks_harness.engine.auth import PersonaAuthRegistry
from eks_harness.engine.cache import CacheService, ContentResolver
from eks_harness.engine.debugger import Debugger
from eks_harness.engine.drivers import DriverPorts, DriverProvider
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.events import RunEvents
from eks_harness.engine.executors import (
    DaemonArtifacts,
    ExecContext,
    ExecResult,
    ExecutorChoice,
    ExecutorRegistry,
    ExecutorSpec,
    ResolveContext,
)
from eks_harness.engine.journal import Journal, RunJournal
from eks_harness.engine.model import (
    FINAL_NODE_STATUSES,
    BackendHandle,
    BackendIdentity,
    ControlRequest,
    EnvironmentOut,
    ExpansionRecord,
    FakeAnswer,
    FakeProfile,
    LeaseHandle,
    NodeError,
    NodeEventRecord,
    NodeRecord,
    RunClock,
    RunInfo,
    RunOptions,
    RunResources,
    sessions_of,
)
from eks_harness.engine.resources import SlotManager
from eks_harness.engine.sandbox import SandboxLimits, SandboxRunner
from eks_harness.engine.scheduler import (
    TIMEOUT_GRACE_SECONDS,
    RunOutcome,
    Scheduler,
    SchedulerConfig,
    SchedulerDeps,
)
from eks_harness.ids import new_ulid
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.hashing import hash_plan, node_seed, resource_id
from eks_harness.ir.plan import Node, Plan, Resource, SessionSpec
from eks_harness.ir.refs import RJson
from eks_harness.ir.registry import OpSpec, Registry, default_registry
from eks_harness.ir.time import TempoMap
from eks_harness.ops import register_builtin_executors
from eks_harness.ops.fakes import (
    FAKE_PLATFORMS,
    FakeDriverPort,
    FakeDriverProvider,
    FakeExecutors,
)

PROJECT = "acme/scheduler"
SESSION = "scheduler-tests"
PROJECT_DIGEST = "sha256:" + "7" * 64
TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}
PROBE_OPS = ("probe.echo", "probe.pure", "probe.idem", "probe.lease", "probe.render", "probe.blend",
             "probe.scene", "probe.backend", "probe.timed", "probe.shot", "probe.machine", "probe.idlease")
LEASE_PROBES = frozenset({"probe.lease", "probe.idlease"})


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        register_probes()
        yield chosen
    finally:
        chosen.restore(state)


def register_probes() -> None:
    @eks.op("probe", name="echo", out="json", description="Echo a value")
    class Echo(eks.Args):
        value: RJson = None
        mode: str = "echo"
        seconds: float = 0.0
        code: str = "probe_failure"
        retryable: bool = False
        soft: bool = False
        times: int = 0
        key: str = ""

    @eks.op("probe", name="pure", out="json", pure=True, description="A pure probe")
    class Pure(Echo):
        pass

    @eks.op("probe", name="idem", out="json", idempotent=True, description="An idempotent probe")
    class Idem(Echo):
        pass

    @eks.op("probe", name="lease", out="json", resources=("lease",), ordering="graph", platforms=("web",),
            description="A graph-ordered probe on the lease")
    class LeaseProbe(Echo):
        pass

    @eks.op("probe", name="idlease", out="json", resources=("lease",), ordering="graph", platforms=("web",),
            idempotent=True, description="An idempotent graph-ordered probe on the lease")
    class IdLease(Echo):
        pass

    @eks.op("probe", name="render", out="json", location="node", description="A render probe")
    class Render(Echo):
        pass

    @eks.op("probe", name="blend", out="json", location="blender", description="A blender probe")
    class Blend(Echo):
        pass

    @eks.op("probe", name="scene", out="json", location="browser_scene", description="A scene probe")
    class Scene(Echo):
        pass

    @eks.op("probe", name="backend", out="json", resources=("backend",), description="A backend probe")
    class BackendProbe(Echo):
        pass

    @eks.op("probe", name="timed", out="json", description="A probe with a timeout")
    class Timed(Echo):
        timeout: Duration | None = None

    @eks.op("probe", name="shot", out="artifact", pure=True, description="A pure probe writing a file")
    class Shot(Echo):
        pass

    @eks.op("probe", name="machine", out="json", pure=True, device_dependent=True,
            description="A machine-dependent probe")
    class Machine(Echo):
        pass


class Digests:
    def __init__(self, project: str | None = PROJECT_DIGEST) -> None:
        self.project_digest = project

    def plugin(self, plugin_id: str) -> str | None:
        return None

    def project(self) -> str | None:
        return self.project_digest


@dataclass
class Call:
    op: str
    node: str
    instance: str
    attempt: int
    started: float
    ended: float = 0.0
    remaining: float | None = None


class Probe:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[Call] = []
        self.active = 0
        self.peak = 0
        self.lease_active = 0
        self.lease_peak = 0
        self.counters: dict[str, int] = {}
        self.contexts: list[ExecContext] = []
        self.release = threading.Event()
        self.barrier: threading.Barrier | None = None
        self.resolution: dict[str, JsonValue] | None = None
        self.predicted: dict[str, JsonValue] = {}

    def count(self, key: str) -> int:
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + 1
            return self.counters[key]

    def run(self, call: ExecContext) -> ExecResult:
        record = Call(call.op, call.node.id, call.instance, call.attempt, time.monotonic(),
                      remaining=call.remaining())
        uses_lease = call.op in LEASE_PROBES
        with self.lock:
            self.calls.append(record)
            self.contexts.append(call)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if uses_lease:
                self.lease_active += 1
                self.lease_peak = max(self.lease_peak, self.lease_active)
        try:
            return self.behave(call)
        finally:
            with self.lock:
                self.active -= 1
                if uses_lease:
                    self.lease_active -= 1
                record.ended = time.monotonic()

    def behave(self, call: ExecContext) -> ExecResult:
        data = call.data
        mode = data.get("mode", "echo")
        value = data.get("value")
        seconds = float(data.get("seconds", 0.0) or 0.0)
        if mode == "sleep":
            if call.cancel.wait(seconds):
                call.check_cancelled()
            return ExecResult(value=value)
        if mode == "barrier":
            barrier = self.barrier
            if barrier is not None:
                try:
                    barrier.wait(timeout=seconds or 0.3)
                except threading.BrokenBarrierError:
                    pass
            time.sleep(0.05)
            return ExecResult(value=value)
        if mode == "hold":
            while not self.release.is_set():
                call.check_cancelled()
                time.sleep(0.01)
            return ExecResult(value=value)
        if mode == "stuck":
            self.release.wait(10)
            return ExecResult(value=value)
        if mode == "deadline":
            while True:
                call.check_cancelled()
                time.sleep(0.01)
        if mode == "fail":
            raise NodeFailure(str(data.get("code")), f"probe {call.node.id} failed", retryable=bool(data["retryable"]),
                              soft=bool(data["soft"]))
        if mode == "flaky":
            if call.attempt <= int(data.get("times", 0) or 0):
                raise NodeFailure("probe_flaky", f"flaky attempt {call.attempt}", retryable=True)
            return ExecResult(value={"attempt": call.attempt})
        if mode == "count":
            return ExecResult(value=self.count(str(data.get("key") or call.node.id)))
        if mode == "soft":
            return ExecResult(value=value, soft=[NodeError(code="probe_soft", message="kept with a value")])
        if mode == "file":
            artifact = call.artifacts.write(f"shot {value}".encode(), filename="shot.txt", kind="file")
            return ExecResult(value=artifact.model_dump(mode="json", by_alias=True))
        if mode == "resolution":
            return ExecResult(value=value, resolution=self.resolution)
        return ExecResult(value=value)

    def resolve(self, context: ResolveContext) -> Mapping[str, JsonValue]:
        return dict(self.predicted)

    def specs(self, *, resolver: bool = False) -> list[ExecutorSpec]:
        return [ExecutorSpec("test.probe", "1", ops=tuple(op for op in PROBE_OPS if op != "probe.render"
                                                          and op != "probe.blend" and op != "probe.scene"),
                             run=self.run, resolver=self.resolve if resolver else None),
                ExecutorSpec("test.render", "1", ops=("probe.render", "probe.blend", "probe.scene"), run=self.run)]

    def ops(self, op: str) -> list[Call]:
        return [call for call in self.calls if call.op == op]


def ensure_target(db: Database) -> int:
    with db.transaction() as conn:
        projects.ensure(conn, PROJECT)
        session, _ = sessions.ensure_in_project(conn, PROJECT, SESSION)
    return session.id


def lease_handles(plan: Plan) -> dict[str, LeaseHandle]:
    handles: dict[str, LeaseHandle] = {}
    for resource in plan.resources:
        if resource.kind.startswith("lease."):
            platform = resource.kind.removeprefix("lease.")
            handles[resource.id] = LeaseHandle.model_validate({
                "resource_id": resource.id, "kind": resource.kind, "platform": platform,
                "session_key": resource.session or "main", "app": resource.app, "sid": f"sid-{resource.id}",
                "project": PROJECT, "session": SESSION})
    return handles


def backend_identity() -> BackendIdentity:
    return BackendIdentity(definition="testapi", backend_id="testapi@local", lease_id="bl_test", source="request",
                           fingerprint="fp-1", commit="abc", branch="main", version="1.2.3", generation=2)


def backend_handles(plan: Plan) -> dict[str, BackendHandle]:
    return {resource.id: BackendHandle(resource_id=resource.id, session_key=resource.session, url="http://127.0.0.1:9",
                                       identity=backend_identity())
            for resource in plan.resources if resource.kind == "backend"}


@dataclass
class Rig:
    ctx: AppContext
    db: Database
    config: Config
    tmp_path: Path
    plan: Plan
    probe: Probe = field(default_factory=Probe)
    fakes: FakeProfile | None = None
    options: RunOptions = field(default_factory=RunOptions)
    settings: SchedulerConfig = field(default_factory=lambda: SchedulerConfig(parallelism=1, cancel_grace=2.0))
    debug: bool = False
    clock: RunClock | None = None
    resolver: bool = False
    sandbox: SandboxRunner | None = None
    providers: Mapping[str, DriverProvider] | None = None
    resources: RunResources | None = None
    digests: Digests = field(default_factory=Digests)
    real: Callable[[ExecutorRegistry], None] | None = None
    run_id: str = field(default_factory=new_ulid)

    def __post_init__(self) -> None:
        session_id = ensure_target(self.db)
        registry = default_registry()
        self.registry = registry
        self.info = RunInfo(id=self.run_id, project=PROJECT, session=SESSION, session_id=session_id,
                            tree=None, plan_hash=self.plan.hash, plan_kind=self.plan.kind, plan_name=self.plan.name,
                            options=self.options, actor="tester", fakes=self.fakes, clock=self.clock,
                            created_at=time.time())
        self.journal = Journal(self.db)
        self.journal.create_run(self.info, self.plan, request={}, environment=EnvironmentOut(),
                                simulated=self.fakes is not None)
        self.writer = RunJournal(self.journal, RunEvents(self.ctx.events), self.info)
        self.writer.status("running")
        self.artifacts = DaemonArtifacts(self.db, self.config, self.ctx.events)
        self.cache = CacheService(self.config.paths.cache_dir, self.db, max_bytes=1 << 30, max_age_days=30)
        self.content = ContentResolver(self.artifacts, tree=self.tmp_path, base=".", plugin_roots={},
                                       scratch=self.tmp_path / "content")
        self.executors = ExecutorRegistry()
        register_builtin_executors(self.executors, PersonaAuthRegistry(), api_timeout=5.0)
        for spec in self.probe.specs(resolver=self.resolver):
            self.executors.register(spec)
        if self.real is not None:
            self.real(self.executors)
        self.fake_source = FakeExecutors(self.fakes, registry) if self.fakes is not None else None
        sessions_map = sessions_of(self.plan)
        providers: Mapping[str, DriverProvider]
        if self.providers is not None:
            providers = self.providers
        elif self.fakes is not None:
            provider = FakeDriverProvider(self.fakes, registry, self.tmp_path / "ports")
            providers = dict.fromkeys(FAKE_PLATFORMS, provider)
        else:
            providers = {}
        self.ports = DriverPorts(providers, self.info, sessions_map)
        held = self.resources or RunResources(leases=lease_handles(self.plan), backends=backend_handles(self.plan))
        enabled = self.debug or bool(self.options.breakpoints) or self.options.break_on_error
        self.debugger = Debugger(enabled=enabled, status=self.current_status, breakpoints=self.options.breakpoints,
                                 break_on_error=self.options.break_on_error,
                                 journal=lambda type, detail: self.writer.event(type, detail=detail) and None)
        self.deps = SchedulerDeps(
            journal=self.writer, cache=self.cache, content=self.content, plugins=self.digests,
            artifacts=self.artifacts, resources=held, slots=SlotManager(), drivers=self.ports,
            sandbox=self.sandbox or SandboxRunner(limits=SandboxLimits(timeout_seconds=60),
                                                  scratch=self.tmp_path / "sandbox"),
            debugger=self.debugger, choose=self.choose, digests={})
        choices: dict[str, ExecutorChoice] = {}
        for node, _ancestors in self.plan.walk():
            if registry.has_op(node.op) and node.op not in choices:
                chosen = self.choose(registry.op(node.op))
                if chosen is not None:
                    choices[node.op] = chosen
        self.scheduler = Scheduler(self.info, self.plan, registry, choices, sessions_map, self.deps, self.settings)
        self.outcome: RunOutcome | None = None

    def choose(self, spec: OpSpec) -> ExecutorChoice | None:
        return self.executors.choose(spec, fakes=self.fakes, fake_source=self.fake_source)

    def current_status(self) -> Any:
        return self.scheduler.status().status

    def run(self) -> RunOutcome:
        self.outcome = self.scheduler.run()
        return self.outcome

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread

    def nodes(self) -> list[NodeRecord]:
        return self.journal.nodes(self.run_id)

    def node(self, node_id: str, instance: str = "") -> NodeRecord:
        found = self.journal.node(self.run_id, node_id, instance)
        assert found is not None, f"no journaled instance {node_id}@{instance}"
        return found

    def events(self, type: str | None = None, node_id: str | None = None) -> list[NodeEventRecord]:
        found = self.journal.events(self.run_id, limit=100_000)
        return [event for event in found
                if (type is None or event.type == type) and (node_id is None or event.node_id == node_id)]

    def expansions(self) -> list[ExpansionRecord]:
        return self.journal.expansions(self.run_id)

    def wait_paused(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.scheduler.status().pause is not None:
                return
            time.sleep(0.01)
        raise AssertionError(f"the run did not pause: {self.scheduler.status()}")


@pytest.fixture
def make_rig(ctx: AppContext, db: Database, config: Config, tmp_path: Path) -> Callable[..., Rig]:
    def build(plan: Plan, **kwargs: Any) -> Rig:
        return Rig(ctx=ctx, db=db, config=config, tmp_path=tmp_path, plan=plan, **kwargs)

    return build


def web_flow(body: Callable[[eks.Session], Any], name: str = "probe") -> Plan:
    flow = eks.flow(app="web-app", platform="web", name=name)(body)
    return eks.plan(flow)


def ops_of(plan: Plan) -> dict[str, list[Node]]:
    found: dict[str, list[Node]] = {}
    for node, _ancestors in plan.walk():
        found.setdefault(node.op, []).append(node)
    return found


def statuses(rig: Rig) -> dict[str, str]:
    return {f"{record.node_id}@{record.instance}" if record.instance else record.node_id: record.status
            for record in rig.nodes()}


def test_worker_flow_runs_in_program_order(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.record_op("ui.nav", {"route": "/a"})
        s.record_op("ui.tap", TAP)
        text = s.record_op("ui.text", {"target": TAP["target"]})
        s.record_op("ui.fill", {"target": TAP["target"], "value": text})
        return {"text": text}

    plan = web_flow(body)
    rig = make_rig(plan, fakes=FakeProfile(families=["*"]))
    outcome = rig.run()
    assert outcome.status == "passed" and outcome.reason is None
    order = [node.id for node in plan.nodes]
    started = [event.node_id for event in rig.events("node.started")]
    assert started == order
    passed = [event.node_id for event in rig.events("node.passed")]
    assert passed == order
    sequence = [event.node_id for event in rig.events() if event.type in ("node.started", "node.passed")]
    assert sequence == [node for node in order for _ in range(2)]
    records = rig.nodes()
    assert [record.node_id for record in records] == order
    assert all(record.status == "passed" and record.attempt == 1 for record in records)
    assert all(record.simulated for record in records)
    assert {record.executor for record in records} == {"eks.worker@1"}
    assert outcome.counts == {"passed": 4}
    text_node = plan.nodes[2]
    fill = rig.node(plan.nodes[3].id)
    assert isinstance(fill.args, dict) and fill.args["value"] == rig.node(text_node.id).output
    assert outcome.outputs == {"text": rig.node(text_node.id).output}
    port = rig.ports.port(next(iter(rig.deps.resources.leases.values())))
    assert isinstance(port, FakeDriverPort)
    assert port.calls == [("ui.nav", "run"), ("ui.tap", "run"), ("ui.text", "run"), ("ui.fill", "run")]


def test_graph_ordered_pure_ops_run_up_to_parallelism(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        return [s.record_op("probe.pure", {"value": index, "mode": "barrier", "seconds": 0.4}) for index in range(6)]

    plan = web_flow(body)
    assert all(node.after == [] for node in plan.nodes)
    rig = make_rig(plan, options=RunOptions(cache="off"),
                   settings=SchedulerConfig(parallelism=3, cancel_grace=2.0))
    rig.probe.barrier = threading.Barrier(3)
    outcome = rig.run()
    assert outcome.status == "passed"
    assert outcome.outputs == list(range(6))
    assert rig.probe.peak == 3


def test_lease_mutex_keeps_lease_nodes_apart(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with eks.parallel():
            first = s.record_op("probe.lease", {"value": 1, "mode": "sleep", "seconds": 0.15})
            second = s.record_op("probe.lease", {"value": 2, "mode": "sleep", "seconds": 0.15})
            third = s.record_op("probe.lease", {"value": 3, "mode": "sleep", "seconds": 0.15})
        return [first, second, third]

    plan = web_flow(body)
    (block,) = plan.nodes
    assert block.branches is not None
    lanes = block.branches["body"]
    assert all(node.after == [] for node in lanes)
    assert len({tuple(node.uses) for node in lanes}) == 1
    rig = make_rig(plan, settings=SchedulerConfig(parallelism=4, cancel_grace=2.0))
    outcome = rig.run()
    assert outcome.status == "passed"
    assert outcome.outputs == [1, 2, 3]
    assert rig.probe.lease_peak == 1
    calls = sorted(rig.probe.ops("probe.lease"), key=lambda call: call.started)
    assert all(earlier.ended <= later.started for earlier, later in pairwise(calls))


def test_parallel_lanes_on_one_lease_never_overlap(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with eks.parallel():
            s.record_op("ui.tap", TAP)
            s.record_op("ui.tap", {"target": {"by": "text", "args": {"text": "Sil"}}})

    plan = web_flow(body)
    (block,) = plan.nodes
    assert block.branches is not None
    for node in block.branches["body"]:
        node.after = []
    hash_plan(plan)
    seen: list[tuple[str, float, float]] = []
    lock = threading.Lock()

    class SlowPort(FakeDriverPort):
        def call(self, op: str, args: Mapping[str, JsonValue], **kwargs: Any) -> Any:
            start = time.monotonic()
            time.sleep(0.1)
            reply = super().call(op, args, **kwargs)
            with lock:
                seen.append((op, start, time.monotonic()))
            return reply

    class SlowProvider(FakeDriverProvider):
        def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> Any:
            return SlowPort(lease, session, run, self.profile, self.registry, self.scratch / lease.resource_id)

    profile = FakeProfile(families=["*"])
    provider = SlowProvider(profile, default_registry(), Path("/tmp") / f"eks-ports-{new_ulid()}")
    rig = make_rig(plan, fakes=profile, providers=dict.fromkeys(FAKE_PLATFORMS, provider),
                   settings=SchedulerConfig(parallelism=4, cancel_grace=2.0))
    assert rig.run().status == "passed"
    assert len(seen) == 2
    (_, a_start, a_end), (_, b_start, b_end) = sorted(seen, key=lambda item: item[1])
    assert a_end <= b_start or b_end <= a_start


def test_when_branches_and_skipped_nodes(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        flag = s.record_op("probe.echo", {"value": True})
        with eks.when(flag):
            taken = s.record_op("probe.echo", {"value": "then"})
        with eks.otherwise():
            s.record_op("probe.echo", {"value": "else"})
            with eks.when(flag):
                s.record_op("probe.echo", {"value": "nested"})
        empty = s.record_op("probe.echo", {"value": None})
        with eks.when(empty):
            s.record_op("probe.echo", {"value": "never"})
        return {"taken": taken}

    plan = web_flow(body)
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "passed"
    assert outcome.outputs == {"taken": "then"}
    first_when = plan.nodes[1]
    second_when = plan.nodes[3]
    assert first_when.branches is not None and second_when.branches is not None
    else_nodes = first_when.branches["else"]
    nested_when = else_nodes[1]
    assert nested_when.branches is not None
    assert rig.node(else_nodes[0].id).status == "skipped"
    assert rig.node(nested_when.id).status == "skipped"
    assert rig.node(nested_when.branches["then"][0].id).status == "skipped"
    assert rig.node(second_when.branches["then"][0].id).status == "skipped"
    assert rig.node(first_when.id).status == "passed" and rig.node(first_when.id).output is None
    branches = {record.node_id: record.detail for record in rig.expansions() if record.kind == "branch"}
    assert branches == {first_when.id: {"branch": "then"}, second_when.id: {"branch": None}}
    skipped = [event.node_id for event in rig.events("node.skipped")]
    assert else_nodes[0].id in skipped and nested_when.branches["then"][0].id in skipped
    assert outcome.counts["skipped"] == 4


def test_when_else_branch(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        flag = s.record_op("probe.echo", {"value": 0})
        with eks.when(flag):
            s.record_op("probe.echo", {"value": "then"})
        with eks.otherwise():
            chosen = s.record_op("probe.echo", {"value": "else"})
        return chosen

    plan = web_flow(body)
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "passed" and outcome.outputs == "else"
    when = plan.nodes[1]
    assert [record.detail for record in rig.expansions()] == [{"branch": "else"}]
    assert when.branches is not None
    assert rig.node(when.branches["then"][0].id).status == "skipped"


def test_each_runs_iterations_in_order(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        rows = s.record_op("probe.echo", {"value": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]})
        with eks.each(rows, limit=3) as row:
            s.record_op("probe.echo", {"value": row.id})
            s.record_op("probe.echo", {"value": row})
        none = s.record_op("probe.echo", {"value": []})
        with eks.each(none):
            s.record_op("probe.echo", {"value": "never"})

    plan = web_flow(body)
    rig = make_rig(plan, settings=SchedulerConfig(parallelism=4, cancel_grace=2.0))
    outcome = rig.run()
    assert outcome.status == "passed"
    loop = plan.nodes[1]
    assert loop.branches is not None
    first, second = loop.branches["body"]
    for index in range(3):
        instance = f"{loop.id}#{index}"
        assert rig.node(first.id, instance).output == index + 1
        assert rig.node(second.id, instance).output == {"id": index + 1}
    calls = [call for call in rig.probe.calls if call.instance]
    assert [call.instance for call in calls] == [f"{loop.id}#{index}" for index in range(3) for _ in range(2)]
    assert all(earlier.ended <= later.started for earlier, later in pairwise(calls))
    each = {record.node_id: record.detail for record in rig.expansions() if record.kind == "each"}
    assert each[loop.id] == {"count": 3, "items": [{"id": 1}, {"id": 2}, {"id": 3}]}
    empty_loop = plan.nodes[3]
    assert each[empty_loop.id] == {"count": 0, "items": []}
    assert rig.node(empty_loop.id).status == "passed"
    assert rig.node(loop.id).status == "passed" and rig.node(loop.id).output is None


def test_each_bounds_journaled_items_and_rejects_non_lists(make_rig: Callable[..., Rig]) -> None:
    big = ["x" * 2000 for _ in range(40)]

    def body(s: eks.Session) -> Any:
        rows = s.record_op("probe.echo", {"value": big})
        with eks.each(rows, limit=2):
            s.record_op("probe.echo", {"value": 1})

    rig = make_rig(web_flow(body))
    assert rig.run().status == "passed"
    (expanded,) = [record for record in rig.expansions() if record.kind == "each"]
    assert expanded.detail["count"] == 2

    def large(s: eks.Session) -> Any:
        rows = s.record_op("probe.echo", {"value": big})
        with eks.each(rows):
            s.record_op("probe.echo", {"value": 1})

    rig = make_rig(web_flow(large, name="large"))
    assert rig.run().status == "passed"
    (expanded,) = [record for record in rig.expansions() if record.kind == "each"]
    assert expanded.detail["count"] == 40 and expanded.detail.get("truncated") is True

    def scalar(s: eks.Session) -> Any:
        rows = s.record_op("probe.echo", {"value": {"not": "a list"}})
        with eks.each(rows):
            s.record_op("probe.echo", {"value": 1})

    plan = web_flow(scalar, name="scalar")
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "failed"
    loop = plan.nodes[1]
    record = rig.node(loop.id)
    assert record.status == "failed" and record.error is not None and record.error.code == "each_not_list"
    assert outcome.reason == f"ctl.each {loop.id} failed: {record.error.message}"


def test_macro_output_and_parallel(make_rig: Callable[..., Rig]) -> None:
    @eks.macro(namespace="acme")
    def greet(s: eks.Session, name: str, count: int = 1) -> Any:
        return s.record_op("probe.echo", {"value": name})

    def body(s: eks.Session) -> Any:
        source = s.record_op("probe.echo", {"value": "Ada"})
        greeting = s.acme.greet(source, count=2)
        with eks.parallel():
            s.record_op("probe.echo", {"value": "lane"})
        return {"greeting": greeting}

    plan = web_flow(body)
    macro = plan.nodes[1]
    assert macro.op == "ctl.macro"
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "passed"
    assert rig.node(macro.id).output == {"name": "Ada", "count": 2}
    assert outcome.outputs == {"greeting": "Ada"}
    parallel = plan.nodes[2]
    assert parallel.op == "ctl.parallel" and rig.node(parallel.id).status == "passed"
    assert rig.node(parallel.id).output is None


def test_capture_record_enter_and_exit(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with s.record("demo") as video:
            s.record_op("ui.tap", TAP)
        return video

    plan = web_flow(body)
    rig = make_rig(plan, fakes=FakeProfile(families=["*"]))
    outcome = rig.run()
    assert outcome.status == "passed"
    (record,) = plan.nodes
    port = rig.ports.port(next(iter(rig.deps.resources.leases.values())))
    assert isinstance(port, FakeDriverPort)
    assert port.calls == [("capture.record", "enter"), ("ui.tap", "run"), ("capture.record", "exit")]
    journaled = rig.node(record.id)
    assert journaled.status == "passed" and journaled.executor == "eks.worker.container@1"
    assert isinstance(outcome.outputs, dict) and outcome.outputs["id"] in journaled.artifacts


def test_capture_record_body_failure_fails_the_container(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with s.record("demo"):
            s.record_op("ui.tap", TAP)
            s.record_op("ui.tap", {"target": {"by": "text", "args": {"text": "never"}}})

    plan = web_flow(body)
    (record,) = plan.nodes
    assert record.branches is not None
    tap, later = record.branches["body"]
    profile = FakeProfile(families=["*"], answers={tap.id: FakeAnswer(fail="no such button", code="driver_error")})
    rig = make_rig(plan, fakes=profile)
    outcome = rig.run()
    assert outcome.status == "failed"
    assert outcome.reason == f"ui.tap {tap.id} failed: no such button"
    container = rig.node(record.id)
    assert container.status == "failed" and container.error is not None
    assert container.error.code == "driver_error" and container.error.message == f"body failed at {tap.id}"
    assert rig.node(later.id).status == "cancelled"
    port = rig.ports.port(next(iter(rig.deps.resources.leases.values())))
    assert isinstance(port, FakeDriverPort)
    assert ("capture.record", "exit") not in port.calls


def test_hard_failure_cancels_siblings_and_takes_failure_shots(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        slow = s.record_op("probe.pure", {"value": 1, "mode": "stuck"})
        s.record_op("ui.tap", TAP)
        s.record_op("probe.echo", {"value": 3})
        return slow

    plan = web_flow(body)
    slow, tap, after = plan.nodes
    profile = FakeProfile(families=["*"], answers={tap.id: FakeAnswer(fail="button missing", code="driver_error")})
    rig = make_rig(plan, fakes=profile, options=RunOptions(cache="off"),
                   settings=SchedulerConfig(parallelism=2, cancel_grace=0.3))
    began = time.monotonic()
    try:
        outcome = rig.run()
    finally:
        rig.probe.release.set()
    assert outcome.status == "failed"
    assert outcome.reason == f"ui.tap {tap.id} failed: button missing"
    assert 0.3 <= time.monotonic() - began < 5
    assert rig.node(tap.id).status == "failed"
    assert rig.node(slow.id).status == "cancelled"
    assert rig.node(after.id).status == "cancelled"
    (shot_event,) = rig.events("node.failure_shot", tap.id)
    shots = shot_event.detail["artifacts"]
    assert isinstance(shots, list) and len(shots) == 1
    assert shots[0] in rig.node(tap.id).artifacts
    stored = rig.artifacts.get(str(shots[0]))
    assert stored is not None and stored.kind == "screenshot"
    row = artifacts_repo.get(rig.db.conn(), str(shots[0]))
    assert row is not None and "failure" in row.tags
    assert rig.events("node.cancelled", slow.id)
    assert outcome.counts == {"failed": 1, "cancelled": 2}


def test_retries_only_idempotent_retryable_failures(make_rig: Callable[..., Rig],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "RETRY_BACKOFF_SECONDS", (0.01, 0.02))

    def body(s: eks.Session) -> Any:
        return s.record_op("probe.idem", {"mode": "flaky", "times": 2})

    plan = web_flow(body)
    rig = make_rig(plan, settings=SchedulerConfig(parallelism=1, retries=2, cancel_grace=1.0))
    outcome = rig.run()
    assert outcome.status == "passed" and outcome.outputs == {"attempt": 3}
    (node,) = plan.nodes
    assert rig.node(node.id).attempt == 3
    assert [event.detail["retry"] for event in rig.events("node.retry")] == [1, 2]

    rig = make_rig(web_flow(body, name="exhausted"), settings=SchedulerConfig(parallelism=1, retries=1,
                                                                               cancel_grace=1.0))
    outcome = rig.run()
    assert outcome.status == "failed"
    assert len(rig.events("node.retry")) == 1

    def plain(s: eks.Session) -> Any:
        return s.record_op("probe.echo", {"mode": "flaky", "times": 1})

    rig = make_rig(web_flow(plain, name="plain"), settings=SchedulerConfig(parallelism=1, retries=3,
                                                                            cancel_grace=1.0))
    assert rig.run().status == "failed"
    assert rig.events("node.retry") == []

    def hard(s: eks.Session) -> Any:
        return s.record_op("probe.idem", {"mode": "fail", "code": "probe_failure", "retryable": False})

    rig = make_rig(web_flow(hard, name="hard"), settings=SchedulerConfig(parallelism=1, retries=3, cancel_grace=1.0))
    assert rig.run().status == "failed"
    assert rig.events("node.retry") == []


def test_soft_failures(make_rig: Callable[..., Rig]) -> None:
    def raised(s: eks.Session) -> Any:
        broken = s.record_op("probe.echo", {"mode": "fail", "code": "probe_soft", "soft": True})
        s.record_op("probe.echo", {"value": broken})

    plan = web_flow(raised)
    broken, reader = plan.nodes
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "failed"
    soft = rig.node(broken.id)
    assert soft.status == "soft_failed" and soft.has_output is False and soft.error is not None
    failed = rig.node(reader.id)
    assert failed.error is not None and failed.error.code == "ref_unresolved"
    assert f"node {broken.id} soft-failed and has no output" in failed.error.message

    def kept(s: eks.Session) -> Any:
        return s.record_op("probe.echo", {"mode": "soft", "value": 42})

    plan = web_flow(kept, name="kept")
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "passed" and outcome.outputs == 42
    record = rig.node(plan.nodes[0].id)
    assert record.status == "soft_failed" and record.has_output and record.output == 42
    assert record.error is not None and record.error.code == "probe_soft"
    assert outcome.counts == {"soft_failed": 1}


def test_wrong_output_type_fails_and_is_never_kept(make_rig: Callable[..., Rig]) -> None:
    def flow(s: eks.Session) -> Any:
        return s.record_op("probe.shot", {"value": 1})

    plan = web_flow(flow)
    (node,) = plan.nodes
    profile = FakeProfile(families=["*"], force=["probe.shot"], answers={node.id: FakeAnswer(value="not an artifact")})
    rig = make_rig(plan, fakes=profile)
    outcome = rig.run()
    assert outcome.status == "failed"
    record = rig.node(node.id)
    assert record.error is not None and record.error.code == "output_type"
    assert record.has_output is False and record.output is None
    assert rig.cache.stats().entries == 0
    assert all(event.detail.get("outcome") != "stored" for event in rig.events("node.cache"))

    rig = make_rig(plan, fakes=profile, options=RunOptions(break_on_error=True))
    thread = rig.start()
    rig.wait_paused()
    pause = rig.scheduler.status().pause
    assert pause is not None and pause.reason == "error" and pause.error is not None
    assert pause.error.code == "output_type"
    rig.scheduler.abort("test over")
    thread.join(10)
    assert rig.outcome is not None and rig.outcome.status == "aborted"


def test_timeout_precedence(make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "TIMEOUT_GRACE_SECONDS", 0.2)

    def remaining(rig: Rig) -> float:
        (call,) = rig.probe.ops("probe.timed")
        assert call.remaining is not None
        return call.remaining

    def own(s: eks.Session) -> Any:
        return s.record_op("probe.timed", {"timeout": "3s"})

    rig = make_rig(web_flow(own), options=RunOptions(node_timeout=7.0), settings=SchedulerConfig(node_timeout=9.0))
    assert rig.run().status == "passed"
    assert 2.5 < remaining(rig) <= 3.0

    plan = web_flow(lambda s: s.record_op("probe.timed", {}), name="session")
    assert plan.session is not None
    plan.session = SessionSpec.model_validate({**plan.session.model_dump(mode="json"), "timeout": "5s"})
    rig = make_rig(plan, options=RunOptions(node_timeout=7.0), settings=SchedulerConfig(node_timeout=9.0))
    assert rig.run().status == "passed"
    assert 4.5 < remaining(rig) <= 5.0

    rig = make_rig(web_flow(lambda s: s.record_op("probe.timed", {}), name="option"),
                   options=RunOptions(node_timeout=7.0), settings=SchedulerConfig(node_timeout=9.0))
    assert rig.run().status == "passed"
    assert 6.5 < remaining(rig) <= 7.0

    rig = make_rig(web_flow(lambda s: s.record_op("probe.timed", {}), name="config"),
                   settings=SchedulerConfig(node_timeout=9.0))
    assert rig.run().status == "passed"
    assert 8.5 < remaining(rig) <= 9.0


def test_timeouts_fail_after_the_grace(make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    assert TIMEOUT_GRACE_SECONDS == 5.0
    monkeypatch.setattr(scheduler_module, "TIMEOUT_GRACE_SECONDS", 0.3)

    def stuck(s: eks.Session) -> Any:
        return s.record_op("probe.echo", {"mode": "stuck"})

    plan = web_flow(stuck)
    rig = make_rig(plan, options=RunOptions(node_timeout=0.2), settings=SchedulerConfig(cancel_grace=0.5))
    began = time.monotonic()
    try:
        outcome = rig.run()
    finally:
        rig.probe.release.set()
    elapsed = time.monotonic() - began
    assert outcome.status == "failed"
    record = rig.node(plan.nodes[0].id)
    assert record.error is not None and record.error.code == "timeout"
    assert "abandoned" in record.error.message
    assert 0.45 <= elapsed < 3.0
    assert rig.scheduler.cancel.is_set()

    def honours(s: eks.Session) -> Any:
        return s.record_op("probe.echo", {"mode": "deadline"})

    plan = web_flow(honours, name="honours")
    rig = make_rig(plan, options=RunOptions(node_timeout=0.2))
    outcome = rig.run()
    record = rig.node(plan.nodes[0].id)
    assert outcome.status == "failed" and record.error is not None and record.error.code == "timeout"
    assert "deadline" in record.error.message


def test_seed_plan_runs_extends_first(make_rig: Callable[..., Rig]) -> None:
    @eks.resource("tenant", description="A tenant")
    class Tenant(eks.Resource):
        name: str

    @eks.seed("scheduled", backend="testapi")
    def scheduled(d: eks.Data) -> None:
        d.extend("base")
        d.tenant("harman", name="Harman")
        d.tenant("beyaz", name="Beyaz")

    plan = eks.plan(scheduled)
    extend, first, second = plan.nodes
    assert extend.op == "data.extend" and first.op == "data.create"
    assert first.after == [] and second.after == []
    plan.nodes = [first, second, extend]
    profile = FakeProfile(families=["*"], answers={extend.id: FakeAnswer(delay_ms=150)})
    rig = make_rig(plan, fakes=profile, settings=SchedulerConfig(parallelism=4, cancel_grace=1.0))
    assert rig.run().status == "passed"
    started = [event.node_id for event in rig.events("node.started")]
    finished = [event.node_id for event in rig.events("node.passed")]
    assert finished[0] == extend.id
    assert started.index(first.id) > finished.index(extend.id) - 1
    created = [event for event in rig.events() if event.node_id in (first.id, second.id)
               and event.type == "node.started"]
    extend_done = next(event for event in rig.events("node.passed", extend.id))
    assert all(event.id > extend_done.id for event in created)


def test_seed_resources_run_one_at_a_time_by_phase_then_program_order(make_rig: Callable[..., Rig]) -> None:
    @eks.resource("tenant", description="A tenant")
    class Tenant(eks.Resource):
        name: str

    @eks.resource("audit", phase="schema", description="Written before the API starts")
    class Audit(eks.Resource):
        text: str

    @eks.resource("doc", phase="mongo", keyed=False, positional=("text",), description="Kept outside the snapshot")
    class Doc(eks.Resource):
        text: str

    @eks.seed("ordered", backend="testapi")
    def ordered(d: eks.Data) -> None:
        d.tenant("harman", name="Harman")
        d.doc("first")
        d.audit("boot", text="x")
        d.tenant("beyaz", name="Beyaz")

    plan = eks.plan(ordered)
    harman, doc, audit, beyaz = plan.nodes
    profile = FakeProfile(families=["*"], answers={node.id: FakeAnswer(delay_ms=60) for node in plan.nodes})
    rig = make_rig(plan, fakes=profile, settings=SchedulerConfig(parallelism=4, cancel_grace=1.0))
    assert rig.run().status == "passed"
    started = [event.node_id for event in rig.events("node.started")]
    assert started == [audit.id, harman.id, beyaz.id, doc.id]
    events = [(event.type, event.node_id) for event in rig.events() if event.type in ("node.started", "node.passed")]
    for previous, current in zip(started, started[1:], strict=False):
        assert events.index(("node.passed", previous)) < events.index(("node.started", current))


def test_after_into_an_inactive_branch_deadlocks(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        flag = s.record_op("probe.echo", {"value": False})
        with eks.when(flag):
            s.record_op("probe.echo", {"value": "inside"})
        s.record_op("probe.echo", {"value": "after"})

    plan = web_flow(body)
    _flag, when, after = plan.nodes
    assert when.branches is not None
    inside = when.branches["then"][0]
    when.branches = {"then": []}
    after.after = [inside.id]
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "error"
    assert outcome.reason == f"engine: deadlock at {after.id} waiting for {inside.id}"
    assert rig.node(after.id).status == "cancelled"


def test_abort_and_unresolved_outputs(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        return s.record_op("probe.echo", {"mode": "hold"})

    plan = web_flow(body)
    rig = make_rig(plan, settings=SchedulerConfig(parallelism=1, cancel_grace=1.0))
    thread = rig.start()
    deadline = time.monotonic() + 5
    while not rig.probe.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    threading.Thread(target=rig.scheduler.abort, args=("the user cancelled",)).start()
    thread.join(10)
    assert rig.outcome is not None
    assert rig.outcome.status == "aborted" and rig.outcome.reason == "the user cancelled"
    assert rig.node(plan.nodes[0].id).status == "cancelled"
    assert rig.scheduler.cancel.is_set()

    def missing(s: eks.Session) -> Any:
        broken = s.record_op("probe.echo", {"mode": "fail", "code": "probe_soft", "soft": True})
        return {"broken": broken}

    plan = web_flow(missing, name="missing")
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "passed" and outcome.outputs is None
    (unresolved,) = rig.events("run.outputs_unresolved")
    assert plan.nodes[0].id in str(unresolved.detail["message"])


def test_exec_context_wiring(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.record_op("probe.echo", {"value": 1})
        s.record_op("probe.backend", {"value": 2})
        s.record_op("probe.lease", {"value": 3})

    plan = web_flow(body)
    main = plan.session
    assert main is not None
    take_session = main.model_copy(update={"key": "take0", "persona": "harman-admin"})
    lease = Resource(id=resource_id("lease.web", {"app": "web-app", "session": "take0"}), kind="lease.web",
                     app="web-app", session="take0")
    inner = Node(id="n0000000t01", op="probe.echo", args={"value": "take"}, out="json")
    take = Node(id="n0000000t00", op="score.take", args={"session": take_session.model_dump(mode="json")},
                uses=[lease.id], out="media", branches={"body": [inner]}, after=[plan.nodes[-1].id])
    plan.resources.append(lease)
    plan.nodes.append(take)
    hash_plan(plan)
    clock = RunClock(tempo=TempoMap(bpm=128.0), cues={"drop": "2s"})
    rig = make_rig(plan, fakes=FakeProfile(families=["*"]), clock=clock)
    outcome = rig.run()
    assert outcome.status == "passed"
    contexts = {call.node.id: call for call in rig.probe.contexts}
    echo, backend, lease_probe = (contexts[node.id] for node in plan.nodes[:3])
    assert echo.session == main and echo.state is not None and echo.state.key == "main"
    assert echo.driver is None and echo.simulated is False
    assert echo.seed == node_seed(plan.clock.seed, plan.nodes[0])
    assert echo.clock.tempo == TempoMap(bpm=128.0)
    assert echo.clock.cues["drop"].ticks > 0
    assert lease_probe.driver is not None and lease_probe.driver.simulated
    assert lease_probe.simulated is True and rig.node(plan.nodes[2].id).simulated
    assert backend.backend().identity == backend_identity()
    resolution = rig.node(plan.nodes[1].id).resolution
    assert resolution is not None and resolution["backend"] == backend_identity().model_dump(mode="json")
    in_take = contexts[inner.id]
    assert in_take.session is not None and in_take.session.key == "take0"
    assert in_take.state is not None and in_take.state.persona == "harman-admin"
    take_record = rig.node(take.id)
    assert take_record.status == "passed" and take_record.simulated
    assert take_record.executor == "eks.worker.container@1"


def test_render_locations_take_slots(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.record_op("probe.render", {"value": 1})
        s.record_op("probe.blend", {"value": 2})
        s.record_op("probe.scene", {"value": 3})
        s.record_op("probe.echo", {"value": 4})

    plan = web_flow(body)
    rig = make_rig(plan)
    assert rig.run().status == "passed"
    slots = [record for record in rig.journal.resources(rig.run_id) if record.kind == "slot"]
    assert len(slots) == 3
    assert {record.detail["location"] for record in slots} == {"node", "blender", "browser_scene"}
    assert all(record.state == "released" and record.resource_id.startswith("slot:") for record in slots)
    held = [event for event in rig.events("resource.held")]
    assert len(held) == 3 and all(event.detail["kind"] == "slot" for event in held)
    assert {record.location for record in rig.nodes()} == {"node", "blender", "browser_scene", "hub"}


def lanes(plan: Plan, chains: Mapping[int, int] | None = None) -> list[Node]:
    block = next(node for node in plan.nodes if node.op == "ctl.parallel")
    assert block.branches is not None
    body = block.branches["body"]
    for index, node in enumerate(body):
        follows = (chains or {}).get(index)
        node.after = [body[follows].id] if follows is not None else []
    hash_plan(plan)
    return body


def no_overlap(calls: list[Call]) -> bool:
    ordered = sorted(calls, key=lambda call: call.started)
    return all(earlier.ended <= later.started for earlier, later in pairwise(ordered))


class StalledServer:
    def __init__(self) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = int(self.server.getsockname()[1])
        self.held: list[socket.socket] = []
        self.connected = threading.Event()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.held.append(conn)
            self.connected.set()

    def close(self) -> None:
        self.server.close()
        for conn in self.held:
            conn.close()


def test_abort_ends_a_stalled_download_at_once(make_rig: Callable[..., Rig]) -> None:
    server = StalledServer()
    try:
        def body(s: eks.Session) -> Any:
            source = s.record_op("media.source", {"uri": f"http://127.0.0.1:{server.port}/clip.mp4", "kind": "video"})
            return s.record_op("probe.pure", {"value": source})

        plan = web_flow(body)
        source, _reader = plan.nodes
        rig = make_rig(plan, fakes=FakeProfile(families=["*"]), settings=SchedulerConfig(parallelism=1,
                                                                                          cancel_grace=2.0))
        thread = rig.start()
        assert server.connected.wait(10), "the content check never connected"
        time.sleep(0.2)
        assert rig.scheduler.status().running == (source.id,)
        began = time.monotonic()
        rig.scheduler.abort("the user cancelled the download")
        assert time.monotonic() - began < 1.0
        thread.join(10)
        assert time.monotonic() - began < 3.0
        assert rig.outcome is not None and rig.outcome.status == "aborted"
        assert rig.outcome.reason == "the user cancelled the download"
        record = rig.node(source.id)
        assert record.status == "cancelled" and record.error is not None and record.error.code == "cancelled"
    finally:
        server.close()


def test_retries_wait_for_a_free_worker(make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "RETRY_BACKOFF_SECONDS", (0.01,))
    monkeypatch.setattr(scheduler_module, "TIMEOUT_GRACE_SECONDS", 0.1)

    def body(s: eks.Session) -> Any:
        with eks.parallel():
            flaky = s.record_op("probe.idem", {"mode": "flaky", "times": 1})
            slow = s.record_op("probe.timed", {"mode": "sleep", "seconds": 0.6, "value": "slow", "timeout": "5s"})
        return [flaky, slow]

    plan = web_flow(body)
    flaky, slow = lanes(plan)
    rig = make_rig(plan, options=RunOptions(node_timeout=0.3),
                   settings=SchedulerConfig(parallelism=1, retries=2, cancel_grace=1.0))
    outcome = rig.run()
    assert outcome.status == "passed", outcome.reason
    assert outcome.outputs == [{"attempt": 2}, "slow"]
    assert rig.node(flaky.id).attempt == 2 and rig.node(slow.id).status == "passed"
    assert rig.probe.peak == 1
    (timed,) = rig.probe.ops("probe.timed")
    first, second = sorted(rig.probe.ops("probe.idem"), key=lambda call: call.started)
    assert first.ended <= timed.started and timed.ended <= second.started
    assert second.remaining is not None and second.remaining > 0.2


def test_abandoned_calls_keep_their_lease(make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "TIMEOUT_GRACE_SECONDS", 0.1)

    def body(s: eks.Session) -> Any:
        with eks.parallel():
            s.record_op("probe.lease", {"mode": "stuck", "value": 1})
            s.record_op("probe.lease", {"value": 2})

    plan = web_flow(body)
    stuck, other = lanes(plan)
    rig = make_rig(plan, options=RunOptions(node_timeout=0.2, break_on_error=True),
                   settings=SchedulerConfig(parallelism=2, cancel_grace=1.0))
    thread = rig.start()
    try:
        rig.wait_paused()
        pause = rig.scheduler.status().pause
        assert pause is not None and pause.node == stuck.id
        assert pause.error is not None and pause.error.code == "timeout"
        skipped = rig.scheduler.control(ControlRequest(action="skip", value=1), timeout=5)
        assert skipped.ok, skipped.message
        time.sleep(0.4)
        assert len(rig.probe.ops("probe.lease")) == 1
        assert rig.node(other.id).status == "pending"
    finally:
        rig.probe.release.set()
    thread.join(10)
    assert rig.outcome is not None and rig.outcome.status == "passed"
    assert rig.probe.lease_peak == 1 and no_overlap(rig.probe.ops("probe.lease"))
    logs = [str(event.detail.get("message")) for event in rig.events("node.log", stuck.id)]
    assert any("pause snapshots skipped" in message for message in logs)
    assert any("returned after its timeout" in message for message in logs)
    assert rig.node(stuck.id).status == "skipped" and rig.node(other.id).status == "passed"


class GatedSlots(SlotManager):
    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate

    def acquire(self, location: Any, label: str, *, run_id: str, cancel: threading.Event,
                on_wait: Callable[[int, str], None] | None = None) -> Any:
        if on_wait is not None:
            on_wait(1, "gate")
        while not self.gate.wait(0.01):
            if cancel.is_set():
                raise RuntimeError("cancelled while waiting for the gate")
        return super().acquire(location, label, run_id=run_id, cancel=cancel, on_wait=on_wait)


def test_render_deadline_starts_once_the_slot_is_held(make_rig: Callable[..., Rig],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "TIMEOUT_GRACE_SECONDS", 0.1)

    def body(s: eks.Session) -> Any:
        return s.record_op("probe.render", {"value": 1})

    plan = web_flow(body)
    rig = make_rig(plan, options=RunOptions(node_timeout=0.3))
    gate = threading.Event()
    rig.deps.slots = GatedSlots(gate)
    opener = threading.Timer(0.8, gate.set)
    opener.start()
    began = time.monotonic()
    outcome = rig.run()
    assert outcome.status == "passed", outcome.reason
    assert time.monotonic() - began >= 0.8
    (call,) = rig.probe.ops("probe.render")
    assert call.remaining is not None and call.remaining > 0.2
    states = [record.state for record in rig.journal.resources(rig.run_id) if record.kind == "slot"]
    assert "acquiring" in states and "released" in states


def test_container_exit_waits_for_the_lease(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with eks.parallel():
            with s.record("demo"):
                s.record_op("probe.echo", {"value": 1})
            s.record_op("ui.tap", TAP)

    plan = web_flow(body)
    record, tap = lanes(plan)
    seen: list[tuple[str, str, float, float]] = []
    lock = threading.Lock()

    class SlowPort(FakeDriverPort):
        def call(self, op: str, args: Mapping[str, JsonValue], **kwargs: Any) -> Any:
            start = time.monotonic()
            time.sleep(0.4 if op == "ui.tap" else 0.1)
            reply = super().call(op, args, **kwargs)
            with lock:
                seen.append((op, str(kwargs.get("phase", "run")), start, time.monotonic()))
            return reply

    class SlowProvider(FakeDriverProvider):
        def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> Any:
            return SlowPort(lease, session, run, self.profile, self.registry, self.scratch / lease.resource_id)

    profile = FakeProfile(families=["*"])
    provider = SlowProvider(profile, default_registry(), Path("/tmp") / f"eks-ports-{new_ulid()}")
    rig = make_rig(plan, fakes=profile, providers=dict.fromkeys(FAKE_PLATFORMS, provider),
                   settings=SchedulerConfig(parallelism=4, cancel_grace=2.0))
    assert rig.run().status == "passed"
    assert rig.node(record.id).status == "passed" and rig.node(tap.id).status == "passed"
    ordered = sorted(seen, key=lambda item: item[2])
    assert [(op, phase) for op, phase, _, _ in ordered] == [
        ("capture.record", "enter"), ("ui.tap", "run"), ("capture.record", "exit")]
    assert all(earlier[3] <= later[2] for earlier, later in pairwise(ordered))


def test_ending_from_a_pause_does_not_spin(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with eks.parallel():
            s.record_op("probe.echo", {"mode": "stuck"})
            s.record_op("probe.echo", {"value": 2})

    plan = web_flow(body)
    stuck, second = lanes(plan)
    rig = make_rig(plan, options=RunOptions(breakpoints=[second.id]),
                   settings=SchedulerConfig(parallelism=2, cancel_grace=1.0))
    thread = rig.start()
    try:
        rig.wait_paused()
        deadline = time.monotonic() + 5
        while not rig.probe.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rig.probe.calls and rig.probe.calls[0].node == stuck.id
        used = time.process_time()
        began = time.monotonic()
        answer = rig.scheduler.control(ControlRequest(action="abort"), timeout=5)
        assert answer.ok
        thread.join(10)
        elapsed = time.monotonic() - began
        used = time.process_time() - used
    finally:
        rig.probe.release.set()
    assert rig.outcome is not None and rig.outcome.status == "aborted"
    assert elapsed >= 0.9
    assert used < 0.5, f"the loop used {used:.2f} s of CPU while it waited {elapsed:.2f} s"


class BrokenJournal(RunJournal):
    def expansion(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the expansion table is gone")


def test_engine_errors_end_the_run_with_error(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        flag = s.record_op("probe.echo", {"value": True})
        with eks.when(flag):
            s.record_op("probe.echo", {"value": "then"})
        s.record_op("probe.echo", {"value": "after"})

    plan = web_flow(body)
    flag, when, after = plan.nodes
    rig = make_rig(plan)
    rig.deps.journal = BrokenJournal(rig.journal, RunEvents(rig.ctx.events), rig.info)
    outcome = rig.run()
    assert outcome.status == "error"
    assert outcome.reason == "engine: RuntimeError: the expansion table is gone"
    assert outcome.outputs is None
    assert all(record.status in FINAL_NODE_STATUSES for record in rig.nodes())
    assert rig.node(flag.id).status == "passed"
    assert rig.node(when.id).status == "cancelled" and rig.node(after.id).status == "cancelled"
