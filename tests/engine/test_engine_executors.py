from __future__ import annotations

import hashlib
import sys
import textwrap
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, JsonValue

import eks
from eks_harness.config import Config
from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import projects, sessions
from eks_harness.engine.drivers import (
    CallPhase,
    CallSite,
    DriverFile,
    DriverPort,
    DriverPorts,
    DriverRegistry,
    DriverReply,
    SnapshotKind,
)
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.executors import (
    MATCH_ANY,
    MATCH_EXACT,
    MATCH_FAMILY,
    NATIVE_EXECUTOR,
    NATIVE_OPS,
    NATIVE_VERSION,
    NO_MATCH,
    ArtifactSink,
    BodyOutcome,
    ContainerEntry,
    DaemonArtifacts,
    ExecContext,
    ExecResult,
    ExecutorRegistry,
    ExecutorSpec,
    SessionState,
    as_result,
    native_executor,
    pattern_matches,
)
from eks_harness.engine.model import (
    BackendHandle,
    BackendIdentity,
    FakeProfile,
    LeaseHandle,
    RunInfo,
    RunResources,
)
from eks_harness.ir.plan import Node, Plan, SessionSpec
from eks_harness.ir.registry import (
    OpSpec,
    Provenance,
    Registry,
    contributing,
    default_registry,
)
from eks_harness.ir.types import Artifact
from eks_harness.ops.fakes import FAKE_EXECUTOR, FakeExecutors

PROJECT = "acme/engine"
SESSION = "engine-tests"
TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}
PLUGIN_MODULE = "eks_engine_executor_plugin"


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, JsonValue]]] = []
        self.progress_calls: list[tuple[float, str]] = []
        self.artifacts: list[Artifact] = []

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.append((type, dict(detail)))

    def progress(self, fraction: float, message: str) -> None:
        self.progress_calls.append((fraction, message))

    def artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: dict[str, tuple[Artifact, Path]] = {}
        self.calls: list[dict[str, Any]] = []
        self.scratches = 0

    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact:
        data = path.read_bytes()
        artifact_id = f"A{len(self.rows) + 1:03d}"
        stored = self.root / "store" / artifact_id
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(data)
        if move:
            path.unlink()
        self.calls.append({"project": project, "session": session, "kind": kind, "name": name, "tags": list(tags),
                           "meta": dict(meta), "actor": actor, "move": move})
        artifact = Artifact(id=artifact_id, kind=kind or "file", name=name or path.name, size=len(data),
                            sha256="sha256:" + hashlib.sha256(data).hexdigest())
        self.rows[artifact_id] = (artifact, stored)
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        found = self.rows.get(artifact_id)
        return found[0] if found else None

    def file(self, artifact_id: str) -> Path | None:
        found = self.rows.get(artifact_id)
        return found[1] if found else None

    def scratch(self) -> Path:
        self.scratches += 1
        directory = self.root / f"scratch-{self.scratches}"
        directory.mkdir(parents=True)
        return directory


def run_info(plan: Plan, actor: str | None = "tester") -> RunInfo:
    return RunInfo(id="01JENGINETESTRUN000000000", project=PROJECT, session=SESSION, plan_hash=plan.hash,
                   plan_kind=plan.kind, plan_name=plan.name, actor=actor, created_at=time.time())


def flow_plan() -> Plan:
    @eks.flow(app="web-app", platform="web")
    def sample(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)
        s.record_op("api.get", {"path": "/api/branches"})
        s.record_op("expect.value", {"value": 1, "assertion": {"kind": "truthy"}})

    return eks.plan(sample)


def node_of(plan: Plan, op: str) -> Node:
    return next(node for node, _ancestors in plan.walk() if node.op == op)


def make_call(plan: Plan, node: Node, *, store: Any, sink: RecordingSink | None = None,
              resources: RunResources | None = None, deadline: float | None = None,
              cancel: threading.Event | None = None, reread: Any = None, registry: Registry | None = None,
              attempt: int = 1, driver: DriverPort | None = None) -> ExecContext:
    chosen = registry or default_registry()
    spec = chosen.op(node.op)
    model = spec.args.model_validate(node.args)
    data = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    info = run_info(plan)
    recording = sink or RecordingSink()
    artifacts = ArtifactSink(store, run=info, node_id=node.id, instance="", op=node.op, sink=recording)
    return ExecContext(run=info, plan=plan, registry=chosen, node=node, spec=spec, instance="", attempt=attempt,
                       args=model, data=data, session=plan.session, state=SessionState("main"),
                       resources=resources or RunResources(), driver=driver, sink=recording, artifacts=artifacts,
                       seed=0, clock=plan.clock, deadline=deadline, cancel=cancel or threading.Event(),
                       reread=reread)


class Args(BaseModel):
    value: int = Field(default=0, description="value")


def op_spec(name: str, *, location: Any = "hub", container: bool = False, executor: str | None = None,
            executor_version: str = "", provenance: Provenance | None = None) -> OpSpec:
    family = name.split(".", 1)[0]
    return OpSpec(name=name, family=family, args=Args, location=location, container=container,
                  branches=("body",) if container else (), executor=executor, executor_version=executor_version,
                  provenance=provenance or Provenance("project", ""))


def noop(call: ExecContext) -> ExecResult:
    return ExecResult(value=call.op)


class Box:
    def enter(self, call: ExecContext, /) -> ContainerEntry:
        return ContainerEntry()

    def exit(self, call: ExecContext, body: BodyOutcome, /) -> ExecResult:
        return ExecResult()


def test_pattern_matches_exact_family_any_and_none() -> None:
    tap = default_registry().op("ui.tap")
    assert pattern_matches("ui.tap", tap) == MATCH_EXACT
    assert pattern_matches("ui", tap) == MATCH_FAMILY
    assert pattern_matches("ui.*", tap) == MATCH_FAMILY
    assert pattern_matches("*", tap) == MATCH_ANY
    assert pattern_matches("ui.fill", tap) == NO_MATCH
    assert pattern_matches("web.*", tap) == NO_MATCH
    assert pattern_matches("u", tap) == NO_MATCH


def test_spec_match_honours_locations_and_kinds() -> None:
    registry = default_registry()
    tap = registry.op("ui.tap")
    record = registry.op("capture.record")
    api = registry.op("api.get")
    plain = ExecutorSpec("t.plain", "1", ops=("ui.tap", "ui.*", "*"), run=noop)
    assert plain.match(tap) == MATCH_EXACT
    assert plain.match(api) == MATCH_ANY
    assert plain.match(record) == NO_MATCH
    worker = ExecutorSpec("t.worker", "1", ops=("*",), run=noop, locations=frozenset({"worker"}))
    assert worker.match(tap) == MATCH_ANY
    assert worker.match(api) == NO_MATCH
    container = ExecutorSpec("t.box", "1", ops=("*",), container=Box())
    assert container.match(record) == MATCH_ANY
    assert container.match(tap) == NO_MATCH
    assert plain.identity == "t.plain@1"


def test_register_refuses_bad_specs_and_duplicates() -> None:
    executors = ExecutorRegistry()
    with pytest.raises(ValueError, match="name"):
        executors.register(ExecutorSpec("", "1", ops=("ui.tap",), run=noop))
    with pytest.raises(ValueError, match="version"):
        executors.register(ExecutorSpec("t.a", "", ops=("ui.tap",), run=noop))
    with pytest.raises(ValueError, match="exactly one"):
        executors.register(ExecutorSpec("t.a", "1", ops=("ui.tap",)))
    with pytest.raises(ValueError, match="exactly one"):
        executors.register(ExecutorSpec("t.a", "1", ops=("ui.tap",), run=noop, container=Box()))
    first = executors.register(ExecutorSpec("t.a", "1", ops=("ui.tap", "ui.fill"), run=noop))
    with pytest.raises(ValueError, match=r"t\.a is already registered for ui\.fill"):
        executors.register(ExecutorSpec("t.a", "2", ops=("ui.fill",), run=noop))
    other = executors.register(ExecutorSpec("t.a", "2", ops=("ui.type",), run=noop))
    assert executors.specs() == [first, other]


def test_real_takes_the_best_match_and_the_later_one_on_ties() -> None:
    registry = default_registry()
    tap = registry.op("ui.tap")
    executors = ExecutorRegistry()
    assert executors.real(tap) is None
    anything = executors.register(ExecutorSpec("t.any", "1", ops=("*",), run=noop))
    assert executors.real(tap) is anything
    family = executors.register(ExecutorSpec("t.family", "1", ops=("ui.*",), run=noop))
    exact = executors.register(ExecutorSpec("t.exact", "1", ops=("ui.tap",), run=noop))
    later_family = executors.register(ExecutorSpec("t.family2", "1", ops=("ui",), run=noop))
    assert executors.real(tap) is exact
    assert executors.real(registry.op("ui.fill")) is later_family
    assert family is not later_family
    later_exact = executors.register(ExecutorSpec("t.exact2", "1", ops=("ui.tap",), run=noop))
    assert executors.real(tap) is later_exact
    assert executors.real(registry.op("capture.record")) is None


@pytest.fixture
def plugin_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    root = tmp_path / "plugin-src"
    root.mkdir()
    (root / f"{PLUGIN_MODULE}.py").write_text(textwrap.dedent("""
        from eks_harness.engine.executors import ContainerEntry, ExecResult

        def fn(call):
            return {"op": call.op, "data": call.data}

        def nothing(call):
            return None

        class Klass:
            def __call__(self, call):
                return ExecResult(value=[1, 2], detail={"klass": True})

        class Modelled:
            def __call__(self, call):
                from eks_harness.ir.types import Artifact
                return Artifact(id="A1", kind="file", raw_url="http://x/raw")

        class Box:
            def enter(self, call):
                return ["body"]

            def exit(self, call, body):
                return {"status": body.status}

        class Half:
            def enter(self, call):
                return None

        class Broken:
            def __init__(self):
                raise RuntimeError("needs configuration")

        class Weird:
            def __call__(self, call):
                return {1, 2}

        not_callable = 5
        """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    try:
        yield PLUGIN_MODULE
    finally:
        sys.modules.pop(PLUGIN_MODULE, None)


def test_plugin_executor_resolves_functions_classes_and_containers(plugin_module: str, tmp_path: Path) -> None:
    registry = default_registry()

    @eks.op("acmex", name="fn", out="json", executor=f"{plugin_module}:fn")
    class FnArgs(eks.Args):
        value: int = Field(default=0, description="value")

    @eks.op("acmex", name="klass", out="json", executor=f"{plugin_module}:Klass", executor_version="7")
    class KlassArgs(eks.Args):
        pass

    with contributing("acme.tools", "2.1"):
        @eks.op("acmex", name="box", container=True, branches=("body",), out="json", executor=f"{plugin_module}:Box")
        class BoxArgs(eks.Args):
            pass

    executors = ExecutorRegistry()
    fn_op = registry.op("acmex.fn")
    spec = executors.plugin_executor(fn_op)
    assert spec is not None
    assert spec.name == "plugin:project:acmex.fn"
    assert spec.version == "0"
    assert spec.ops == ("acmex.fn",)
    assert spec.provenance == fn_op.provenance
    assert executors.real(fn_op) is not None

    klass = executors.plugin_executor(registry.op("acmex.klass"))
    assert klass is not None and klass.version == "7"

    box_op = registry.op("acmex.box")
    box = executors.plugin_executor(box_op)
    assert box is not None
    assert box.name == "plugin:acme.tools:acmex.box"
    assert box.version == "2.1"
    assert box.run is None and box.container is not None

    @eks.flow(app="web-app", platform="web")
    def uses(s: eks.Session) -> None:
        s.record_op("acmex.fn", {"value": 3})
        s.record_op("acmex.klass", {})

    plan = eks.plan(uses)
    store = MemoryStore(tmp_path)
    call = make_call(plan, node_of(plan, "acmex.fn"), store=store)
    assert spec.run is not None
    assert spec.run(call).value == {"op": "acmex.fn", "data": {"value": 3}}
    assert klass.run is not None
    result = klass.run(make_call(plan, node_of(plan, "acmex.klass"), store=store))
    assert result.value == [1, 2] and result.detail == {"klass": True}
    entered = box.container.enter(call)
    assert entered.branches == ("body",)
    exited = box.container.exit(call, BodyOutcome("passed", ("body",)))
    assert exited.value == {"status": "passed"}


def test_plugin_executor_results_go_through_as_result(plugin_module: str, tmp_path: Path) -> None:
    executors = ExecutorRegistry()
    plan = flow_plan()
    call = make_call(plan, node_of(plan, "ui.tap"), store=MemoryStore(tmp_path))
    nothing = executors.plugin_executor(op_spec("acmey.a", executor=f"{plugin_module}:nothing"))
    assert nothing is not None and nothing.run is not None
    assert nothing.run(call) == ExecResult()
    modelled = executors.plugin_executor(op_spec("acmey.b", executor=f"{plugin_module}:Modelled"))
    assert modelled is not None and modelled.run is not None
    assert modelled.run(call).value == {"id": "A1", "kind": "file", "name": None, "url": None, "raw_url": "http://x/raw",
                                        "session_url": None, "mime": None, "size": None, "sha256": None}
    weird = executors.plugin_executor(op_spec("acmey.c", executor=f"{plugin_module}:Weird"))
    assert weird is not None and weird.run is not None
    with pytest.raises(NodeFailure) as failure:
        weird.run(call)
    assert failure.value.code == "executor_error"


@pytest.mark.parametrize(("reference", "cause"), [
    ("no_colon_here", "is not 'module:qualname'"),
    ("eks_engine_missing_module_xyz:fn", "cannot be imported"),
    (f"{PLUGIN_MODULE}:missing", "'missing' is not defined"),
    (f"{PLUGIN_MODULE}:Broken", "needs configuration"),
    (f"{PLUGIN_MODULE}:not_callable", "not callable"),
])
def test_plugin_executor_reports_bad_references(plugin_module: str, reference: str, cause: str) -> None:
    executors = ExecutorRegistry()
    with pytest.raises(ValueError, match=cause) as problem:
        executors.plugin_executor(op_spec("acmez.op", executor=reference))
    assert reference in str(problem.value)


def test_plugin_container_needs_enter_and_exit(plugin_module: str) -> None:
    executors = ExecutorRegistry()
    with pytest.raises(ValueError, match="has no exit"):
        executors.plugin_executor(op_spec("acmez.box", container=True, executor=f"{plugin_module}:Half"))
    with pytest.raises(ValueError, match="has no enter or exit"):
        executors.plugin_executor(op_spec("acmez.box2", container=True, executor=f"{plugin_module}:fn"))
    assert executors.plugin_executor(op_spec("acmez.plain")) is None


def test_an_ops_own_executor_beats_family_and_any_matches(plugin_module: str) -> None:
    executors = ExecutorRegistry()
    worker = executors.register(ExecutorSpec("eks.worker", "1", ops=("*",), run=noop,
                                             locations=frozenset({"worker"})))
    family = executors.register(ExecutorSpec("t.family", "1", ops=("acmew.*",), run=noop))
    own = op_spec("acmew.own", location="worker", executor=f"{plugin_module}:fn")
    chosen = executors.real(own)
    assert chosen is not None and chosen.name == "plugin:project:acmew.own"
    assert chosen is not worker and chosen is not family
    plain = op_spec("acmew.plain", location="worker")
    assert executors.real(plain) is family
    assert executors.real(op_spec("other.plain", location="worker")) is worker
    exact = executors.register(ExecutorSpec("t.exact", "1", ops=("acmew.own",), run=noop))
    assert executors.real(own) is exact
    choice = executors.choose(op_spec("acmew.forced", location="worker", executor=f"{plugin_module}:fn"))
    assert choice is not None and choice.executor.name == "plugin:project:acmew.forced"


def test_plugin_executor_uses_the_provenance_version() -> None:
    executors = ExecutorRegistry()
    spec = executors.plugin_executor(op_spec("acmez.v", executor="eks_harness.engine.executors:as_result",
                                             provenance=Provenance("acme.tools", "3.4")))
    assert spec is not None
    assert (spec.name, spec.version) == ("plugin:acme.tools:acmez.v", "3.4")


def test_choose_order_and_reasons() -> None:
    registry = default_registry()
    executors = ExecutorRegistry()
    real_tap = executors.register(ExecutorSpec("t.tap", "1", ops=("ui.tap",), run=noop))
    profile = FakeProfile(families=["media", "ui.*"], force=["ui.fill"])
    source = FakeExecutors(profile, registry)

    native = executors.choose(registry.op("ctl.when"), fakes=profile, fake_source=source)
    assert native is not None and native.native and not native.fake
    assert native.executor.name == NATIVE_EXECUTOR and "native" in native.reason

    executors.register(ExecutorSpec("t.fill", "1", ops=("ui.fill",), run=noop))
    forced = executors.choose(registry.op("ui.fill"), fakes=profile, fake_source=source)
    assert forced is not None and forced.fake and forced.executor.name == FAKE_EXECUTOR
    assert "forces 'ui.fill'" in forced.reason

    real = executors.choose(registry.op("ui.tap"), fakes=profile, fake_source=source)
    assert real is not None and real.executor is real_tap and not real.fake
    assert "real executor t.tap@1" in real.reason

    faked = executors.choose(registry.op("media.markers"), fakes=profile, fake_source=source)
    assert faked is not None and faked.fake and "covers 'media'" in faked.reason

    assert executors.choose(registry.op("edit.render"), fakes=profile, fake_source=source) is None
    assert executors.choose(registry.op("media.markers")) is None
    assert executors.choose(registry.op("media.markers"), fakes=profile) is None
    plain = executors.choose(registry.op("ui.fill"))
    assert plain is not None and not plain.fake


def test_native_executor_refuses_to_run(tmp_path: Path) -> None:
    spec = native_executor()
    assert (spec.name, spec.version) == (NATIVE_EXECUTOR, NATIVE_VERSION)
    assert set(spec.ops) == NATIVE_OPS
    plan = flow_plan()
    assert spec.run is not None
    with pytest.raises(NodeFailure, match="native ops run in the scheduler") as failure:
        spec.run(make_call(plan, node_of(plan, "ui.tap"), store=MemoryStore(tmp_path)))
    assert failure.value.code == "executor_error"


def test_as_result_wraps_values() -> None:
    given = ExecResult(value=3, content="sha256:x")
    assert as_result(given) is given
    assert as_result(None) == ExecResult()
    assert as_result({"a": [1, "b", None, True, 1.5]}).value == {"a": [1, "b", None, True, 1.5]}
    assert as_result("text").value == "text"

    class Aliased(BaseModel):
        model_config = {"populate_by_name": True}
        some_value: int = Field(alias="someValue")

    assert as_result(Aliased.model_validate({"someValue": 4})).value == {"someValue": 4}
    with pytest.raises(NodeFailure) as failure:
        as_result(object())
    assert failure.value.code == "executor_error"


def test_exec_context_deadline_and_cancel(tmp_path: Path) -> None:
    plan = flow_plan()
    node = node_of(plan, "ui.tap")
    store = MemoryStore(tmp_path)
    assert make_call(plan, node, store=store).remaining() is None
    soon = make_call(plan, node, store=store, deadline=time.monotonic() + 30)
    remaining = soon.remaining()
    assert remaining is not None and 25 < remaining <= 30
    soon.check_cancelled()
    past = make_call(plan, node, store=store, deadline=time.monotonic() - 1)
    assert past.remaining() == 0.0
    with pytest.raises(NodeFailure) as timed_out:
        past.check_cancelled()
    assert timed_out.value.code == "timeout"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(NodeFailure) as cancelled:
        make_call(plan, node, store=store, cancel=cancel).check_cancelled()
    assert cancelled.value.code == "cancelled"


def test_exec_context_log_event_and_progress(tmp_path: Path) -> None:
    plan = flow_plan()
    sink = RecordingSink()
    call = make_call(plan, node_of(plan, "ui.tap"), store=MemoryStore(tmp_path), sink=sink)
    call.log("hello", detail={"n": 1})
    call.log("careful", level="warning")
    call.event("http", {"status": 200})
    call.event("resource.lost", {"id": "r1"})
    call.event("plain")
    assert sink.events == [
        ("node.log", {"level": "info", "message": "hello", "n": 1}),
        ("node.log", {"level": "warning", "message": "careful"}),
        ("node.http", {"status": 200}),
        ("resource.lost", {"id": "r1"}),
        ("node.plain", {}),
    ]
    assert call.op == "ui.tap"
    call.progress(0.1, "a")
    call.progress(0.2, "dropped")
    call.progress(1.0, "done")
    time.sleep(0.3)
    call.progress(0.5, "later")
    assert sink.progress_calls == [(0.1, "a"), (1.0, "done"), (0.5, "later")]


def test_exec_context_lease_and_backend(tmp_path: Path) -> None:
    plan = flow_plan()
    lease_id = next(resource.id for resource in plan.resources if resource.kind == "lease.web")
    backend_id = next(resource.id for resource in plan.resources if resource.kind == "backend")
    lease = LeaseHandle(resource_id=lease_id, kind="lease.web", platform="web", session_key="main", sid="abc123",
                        project=PROJECT, session=SESSION)
    identity = BackendIdentity(definition="testapi", backend_id="testapi@x", lease_id="bl_1", source="request")
    backend = BackendHandle(resource_id=backend_id, url="http://127.0.0.1:1", identity=identity)
    held = RunResources(leases={lease_id: lease}, backends={backend_id: backend})
    store = MemoryStore(tmp_path)
    tap = make_call(plan, node_of(plan, "ui.tap"), store=store, resources=held)
    api = make_call(plan, node_of(plan, "api.get"), store=store, resources=held)
    assert tap.lease() == lease
    assert api.backend() == backend
    with pytest.raises(NodeFailure, match="uses no backend") as no_backend:
        tap.backend()
    assert no_backend.value.code == "resource_error"
    with pytest.raises(NodeFailure, match="uses no lease"):
        api.lease()
    empty = make_call(plan, node_of(plan, "ui.tap"), store=store)
    with pytest.raises(NodeFailure, match="not held") as not_held:
        empty.lease()
    assert not_held.value.code == "resource_error"
    with pytest.raises(NodeFailure, match="not held"):
        make_call(plan, node_of(plan, "api.get"), store=store).backend()


def test_exec_context_reread(tmp_path: Path) -> None:
    plan = flow_plan()
    node = node_of(plan, "expect.value")
    seen: list[JsonValue] = []

    def reread(expr: JsonValue) -> JsonValue:
        seen.append(expr)
        return {"fresh": True}

    call = make_call(plan, node, store=MemoryStore(tmp_path), reread=reread)
    expr: JsonValue = {"$ref": "n0000000001", "path": []}
    assert call.reread(expr) == {"fresh": True}
    assert seen == [expr]
    without = make_call(plan, node, store=MemoryStore(tmp_path))
    with pytest.raises(NodeFailure, match="re-reads need the scheduler") as failure:
        without.reread(expr)
    assert failure.value.code == "assertion"


def ensure_target(db: Database) -> None:
    with db.transaction() as conn:
        projects.ensure(conn, PROJECT)
        sessions.ensure_in_project(conn, PROJECT, SESSION)


def test_daemon_artifacts_ingest_get_file_and_scratch(db: Database, config: Config, tmp_path: Path) -> None:
    ensure_target(db)
    store = DaemonArtifacts(db, config)
    source = tmp_path / "shot.txt"
    source.write_bytes(b"hello engine")
    artifact = store.ingest(source, project=PROJECT, session=SESSION, kind="log", name="hello.txt",
                            caption="a caption", tags=["one"], meta={"k": "v"}, actor="tester", move=False)
    assert source.exists()
    assert artifact.kind == "log"
    assert artifact.name == "hello.txt"
    assert artifact.size == len(b"hello engine")
    assert artifact.sha256 == "sha256:" + hashlib.sha256(b"hello engine").hexdigest()
    assert artifact.mime is not None and artifact.mime.startswith("text/")
    assert artifact.url is not None and artifact.url.endswith(f"/a/{artifact.id}")
    assert "/s/" in artifact.url
    assert artifact.raw_url is not None and artifact.raw_url.endswith(f"/raw/{artifact.id}/hello.txt")
    assert artifact.session_url is not None and artifact.url.startswith(artifact.session_url)
    assert store.get(artifact.id) == artifact
    stored = store.file(artifact.id)
    assert stored is not None and stored.read_bytes() == b"hello engine"
    assert store.get("missing") is None
    assert store.file("missing") is None
    first, second = store.scratch(), store.scratch()
    assert first != second and first.is_dir() and second.is_dir()
    assert first.parent == config.paths.tmp_dir / "engine"
    moved = tmp_path / "moved.bin"
    moved.write_bytes(b"\x00\x01")
    store.ingest(moved, project=PROJECT, session=SESSION, kind=None, name=None, caption="", tags=[], meta={},
                 actor=None, move=True)
    assert not moved.exists()


def test_artifact_sink_tags_meta_and_records(db: Database, config: Config) -> None:
    ensure_target(db)
    plan = flow_plan()
    node = node_of(plan, "ui.tap")
    sink = RecordingSink()
    store = DaemonArtifacts(db, config)
    artifacts = ArtifactSink(store, run=run_info(plan), node_id=node.id, instance="each#1", op="ui.tap", sink=sink)
    scratch = store.scratch()
    path = scratch / "evidence.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    ingested = artifacts.ingest(path, kind="file", name="evidence.json", tags=["extra"], meta={"own": 1})
    written = artifacts.write(b"\x89PNG fake", filename="nested/dir/pic.png", kind="screenshot", caption="pic")
    assert artifacts.produced == [ingested.id, written.id]
    assert sink.artifacts == [ingested, written]
    assert written.name == "pic.png"
    assert artifacts.get(written.id) == written
    written_file = artifacts.file(written.id)
    assert written_file is not None and written_file.read_bytes() == b"\x89PNG fake"
    row = artifacts_repo.get(db.conn(), ingested.id)
    assert row is not None
    assert {"extra", "run:01jenginetestrun000000000", f"node:{node.id}", "ui.tap"} <= set(row.tags)
    assert row.meta["run"] == "01JENGINETESTRUN000000000"
    assert row.meta["node"] == node.id
    assert row.meta["instance"] == "each#1"
    assert row.meta["op"] == "ui.tap"
    assert row.meta["own"] == 1
    assert row.created_by == "tester"
    with pytest.raises(ValueError, match="names no file"):
        artifacts.write(b"x", filename="..")


class StubPort:
    def __init__(self, lease: LeaseHandle, session: SessionSpec | None, fail_close: bool = False) -> None:
        self.lease = lease
        self.session = session
        self.fail_close = fail_close
        self.closed = False

    @property
    def platform(self) -> str:
        return self.lease.platform

    @property
    def simulated(self) -> bool:
        return True

    def call(self, op: str, args: Mapping[str, JsonValue], *, site: CallSite, phase: CallPhase = "run",
             timeout: float | None = None) -> DriverReply:
        return DriverReply()

    def snapshot(self, kinds: Sequence[SnapshotKind], dest: Path) -> list[DriverFile]:
        return []

    def version(self) -> str:
        return "stub-1"

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


class StubProvider:
    def __init__(self, name: str, platforms: frozenset[str], fail_close: bool = False) -> None:
        self._name = name
        self._platforms = platforms
        self.fail_close = fail_close
        self.opened: list[StubPort] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def platforms(self) -> frozenset[str]:
        return self._platforms

    def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> DriverPort:
        port = StubPort(lease, session, self.fail_close)
        self.opened.append(port)
        return port


def lease_handle(resource_id: str, platform: Any, session_key: str = "main") -> LeaseHandle:
    return LeaseHandle.model_validate({"resource_id": resource_id, "kind": f"lease.{platform}", "platform": platform,
                                       "session_key": session_key, "sid": f"s{resource_id}", "project": PROJECT,
                                       "session": SESSION})


def test_driver_registry_replaces_platform_providers() -> None:
    drivers = DriverRegistry()
    assert drivers.provider("web") is None
    first = StubProvider("one", frozenset({"web", "ios"}))
    second = StubProvider("two", frozenset({"web"}))
    assert drivers.register(first) is first
    drivers.register(second)
    assert drivers.provider("web") is second
    assert drivers.provider("ios") is first
    assert drivers.platforms() == frozenset({"web", "ios"})
    with pytest.raises(ValueError, match="serves no platform"):
        drivers.register(StubProvider("none", frozenset()))


def test_driver_ports_open_lazily_once_per_lease_and_close_all(caplog: pytest.LogCaptureFixture) -> None:
    plan = flow_plan()
    assert plan.session is not None
    web = StubProvider("web", frozenset({"web"}), fail_close=True)
    ios = StubProvider("ios", frozenset({"ios"}))
    ports = DriverPorts({"web": web, "ios": ios}, run_info(plan), {"main": plan.session})
    assert web.opened == [] and ios.opened == []
    first = ports.port(lease_handle("r1", "web"))
    again = ports.port(lease_handle("r1", "web"))
    other = ports.port(lease_handle("r2", "ios", session_key="take0"))
    assert first is again
    assert len(web.opened) == 1 and len(ios.opened) == 1
    assert web.opened[0].session == plan.session
    assert ios.opened[0].session is None
    assert other is ios.opened[0]
    assert ports.port(lease_handle("r3", "android")) is None
    ports.close()
    assert web.opened[0].closed and ios.opened[0].closed
    assert "close failed" in caplog.text
    assert "lease r1" in caplog.text
    with pytest.raises(DriverError, match="are closed") as refused:
        ports.port(lease_handle("r1", "web"))
    assert refused.value.code == "driver_error"
    assert len(web.opened) == 1


class SlowProvider(StubProvider):
    def __init__(self, name: str, platforms: frozenset[str]) -> None:
        super().__init__(name, platforms)
        self.started = threading.Event()
        self.release = threading.Event()
        self.slow = {"slow"}

    def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> DriverPort:
        if lease.resource_id in self.slow:
            self.started.set()
            assert self.release.wait(10)
        return super().open(lease, session, run)


def test_driver_ports_open_outside_the_lock() -> None:
    plan = flow_plan()
    provider = SlowProvider("web", frozenset({"web"}))
    ports = DriverPorts({"web": provider}, run_info(plan), {"main": plan.session} if plan.session else {})
    results: dict[str, DriverPort | None] = {}

    def open_slow(key: str) -> None:
        results[key] = ports.port(lease_handle("slow", "web"))

    first = threading.Thread(target=open_slow, args=("first",))
    first.start()
    assert provider.started.wait(5)
    second = threading.Thread(target=open_slow, args=("second",))
    second.start()
    started = time.monotonic()
    fast = ports.port(lease_handle("fast", "web"))
    assert fast is not None and time.monotonic() - started < 1.0
    assert second.is_alive()
    provider.release.set()
    first.join(5)
    second.join(5)
    assert results["first"] is results["second"] is not None
    assert [port.lease.resource_id for port in provider.opened] == ["fast", "slow"]
    ports.close()


def test_driver_ports_close_a_port_that_opens_after_close() -> None:
    plan = flow_plan()
    provider = SlowProvider("web", frozenset({"web"}))
    ports = DriverPorts({"web": provider}, run_info(plan), {})
    failures: list[DriverError] = []

    def open_slow() -> None:
        try:
            ports.port(lease_handle("slow", "web"))
        except DriverError as problem:
            failures.append(problem)

    opener = threading.Thread(target=open_slow)
    opener.start()
    assert provider.started.wait(5)
    ports.close()
    provider.release.set()
    opener.join(5)
    assert [problem.code for problem in failures] == ["driver_error"]
    (late,) = provider.opened
    assert late.closed


def test_driver_ports_forget_a_failed_open() -> None:
    plan = flow_plan()

    class Failing(StubProvider):
        def __init__(self) -> None:
            super().__init__("web", frozenset({"web"}))
            self.fail = True

        def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> DriverPort:
            if self.fail:
                self.fail = False
                raise RuntimeError("worker unreachable")
            return super().open(lease, session, run)

    provider = Failing()
    ports = DriverPorts({"web": provider}, run_info(plan), {})
    with pytest.raises(RuntimeError, match="worker unreachable"):
        ports.port(lease_handle("r1", "web"))
    assert ports.port(lease_handle("r1", "web")) is provider.opened[0]
