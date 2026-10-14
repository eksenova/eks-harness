from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from eks_harness.config import Config
from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import projects, sessions
from eks_harness.engine.drivers import (
    CallPhase,
    CallSite,
    DriverFile,
    DriverPort,
    DriverReply,
    FileRole,
    SnapshotKind,
)
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.executors import (
    ArtifactSink,
    BodyOutcome,
    DaemonArtifacts,
    ExecContext,
    SessionState,
)
from eks_harness.engine.model import (
    CheckpointOutcome,
    FakeProfile,
    LeaseHandle,
    RunInfo,
    RunResources,
)
from eks_harness.engine.values import validate_args
from eks_harness.ir.plan import CheckUse, Node, Plan, Resource, SessionSpec
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.types import Artifact, MediaInfo
from eks_harness.ops import WORKER_EXECUTOR
from eks_harness.ops.fakes import FakeDriverPort, png_bytes
from eks_harness.ops.worker import WORKER_LOCATIONS, WorkerExecutor

PROJECT = "acme/worker"
SESSION = "worker-tests"
RUN_ID = "01JWORKERTESTRUN0000000000"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
LEASE_ID = "rlease01"
BACKEND_ID = "rback001"


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
        self.artifacts: list[Artifact] = []

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.append((type, dict(detail)))

    def progress(self, fraction: float, message: str) -> None:
        return None

    def artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)


class SpyPort(FakeDriverPort):
    def __init__(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo, profile: FakeProfile,
                 registry: Registry, scratch: Path) -> None:
        super().__init__(lease, session, run, profile, registry, scratch)
        self.seen: list[tuple[str, dict[str, JsonValue], CallSite, CallPhase, float | None]] = []

    def call(self, op: str, args: Mapping[str, JsonValue], *, site: CallSite, phase: CallPhase = "run",
             timeout: float | None = None) -> DriverReply:
        self.seen.append((op, dict(args), site, phase, timeout))
        return super().call(op, args, site=site, phase=phase, timeout=timeout)


class ScriptedPort:
    def __init__(self, reply: DriverReply | None = None, error: BaseException | None = None) -> None:
        self.reply = reply or DriverReply()
        self.error = error

    @property
    def platform(self) -> str:
        return "web"

    @property
    def simulated(self) -> bool:
        return True

    def call(self, op: str, args: Mapping[str, JsonValue], *, site: CallSite, phase: CallPhase = "run",
             timeout: float | None = None) -> DriverReply:
        if self.error is not None:
            raise self.error
        return self.reply

    def snapshot(self, kinds: Sequence[SnapshotKind], dest: Path) -> list[DriverFile]:
        return []

    def version(self) -> str:
        return "scripted-1"

    def close(self) -> None:
        return None


def session_spec() -> SessionSpec:
    return SessionSpec(app="web-app", platform="web", viewport="desktop",
                       defaults=(CheckUse(kind="console_errors"),), checks=(CheckUse(kind="layout_spill"),))


def make_node(op: str, args: dict[str, JsonValue] | None = None, *, node_id: str = "n0000000001",
              out: str | None = None, branches: dict[str, list[Node]] | None = None) -> Node:
    spec = default_registry().op(op)
    return Node(id=node_id, op=op, args=args or {}, out=out or spec.out, hash=HASH_A, uses=[LEASE_ID],
                branches=branches)


def make_plan(*nodes: Node) -> Plan:
    resources = [
        Resource(id=LEASE_ID, kind="lease.web", app="web-app", session="main"),
        Resource(id=BACKEND_ID, kind="backend", app="web-app", session="main", definition="testapi"),
    ]
    return Plan(kind="flow", name="worker", session=session_spec(), resources=resources, nodes=list(nodes),
                hash=HASH_B)


def run_info(plan: Plan) -> RunInfo:
    return RunInfo(id=RUN_ID, project=PROJECT, session=SESSION, plan_hash=plan.hash, plan_kind=plan.kind,
                   plan_name=plan.name, actor="tester", created_at=time.time())


def lease() -> LeaseHandle:
    return LeaseHandle(resource_id=LEASE_ID, kind="lease.web", platform="web", session_key="main", sid="sid001",
                       project=PROJECT, session=SESSION)


def spy_port(tmp_path: Path) -> SpyPort:
    plan = make_plan()
    return SpyPort(lease(), plan.session, run_info(plan), FakeProfile(), default_registry(), tmp_path / "port")


def make_call(node: Node, store: DaemonArtifacts, driver: DriverPort | None, *, instance: str = "",
              attempt: int = 1, deadline: float | None = None, sink: RecordingSink | None = None) -> ExecContext:
    plan = make_plan(node)
    registry = default_registry()
    spec = registry.op(node.op)
    resolved = validate_args(spec, node.args)
    info = run_info(plan)
    recording = sink or RecordingSink()
    artifacts = ArtifactSink(store, run=info, node_id=node.id, instance=instance, op=node.op, sink=recording)
    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance=instance,
                       attempt=attempt, args=resolved.model, data=resolved.data, session=plan.session,
                       state=SessionState("main"), resources=RunResources(leases={LEASE_ID: lease()}),
                       driver=driver, sink=recording, artifacts=artifacts, seed=0, clock=plan.clock,
                       deadline=deadline, cancel=threading.Event(), simulated=True)


@pytest.fixture
def store(db: Database, config: Config) -> DaemonArtifacts:
    with db.transaction() as conn:
        projects.ensure(conn, PROJECT)
        sessions.ensure_in_project(conn, PROJECT, SESSION)
    return DaemonArtifacts(db, config)


def test_specs() -> None:
    worker = WorkerExecutor()
    plain, container = worker.spec(), worker.container_spec()
    assert (plain.name, plain.ops, plain.locations) == (WORKER_EXECUTOR, ("*",), WORKER_LOCATIONS)
    assert (container.name, container.ops) == (WORKER_EXECUTOR + ".container", ("*",))
    assert container.locations == WORKER_LOCATIONS and container.container is worker
    catalog = default_registry()
    assert plain.match(catalog.op("ui.tap")) == 1
    assert plain.match(catalog.op("capture.record")) == 0
    assert container.match(catalog.op("capture.record")) == 1
    assert plain.match(catalog.op("api.get")) == 0


def test_ui_tap_calls_the_port(tmp_path: Path, store: DaemonArtifacts) -> None:
    port = spy_port(tmp_path)
    node = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "Kaydet"}}})
    call = make_call(node, store, port, instance="n0000000007#1", attempt=2, deadline=time.monotonic() + 30)
    result = WorkerExecutor().run(call)
    assert result.value is None
    assert result.detail == {"fake": True}
    [(op, args, site, phase, timeout)] = port.seen
    assert op == "ui.tap" and args == call.data and phase == "run"
    assert site == CallSite(node, "n0000000007#1", 2)
    assert timeout is not None and 25 < timeout <= 30
    assert call.artifacts.produced == []

    WorkerExecutor().run(make_call(node, store, port))
    assert port.seen[-1][4] is None


def test_capture_shot_ingests_the_value_file(tmp_path: Path, store: DaemonArtifacts, db: Database) -> None:
    port = spy_port(tmp_path)
    node = make_node("capture.shot", {"name": "fatura-ozet"})
    sink = RecordingSink()
    call = make_call(node, store, port, sink=sink)
    result = WorkerExecutor().run(call)
    artifact = Artifact.model_validate(result.value)
    assert result.value == artifact.model_dump(mode="json", by_alias=True)
    assert artifact.name == "fatura-ozet.png" and artifact.kind == "screenshot"
    expected = png_bytes(f"capture.shot:{HASH_A}")
    assert artifact.sha256 == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert call.artifacts.produced == [artifact.id]
    assert sink.artifacts == [artifact]
    assert result.artifacts == []
    stored = store.file(artifact.id)
    assert stored is not None and stored.read_bytes() == expected
    row = artifacts_repo.get(db.conn(), artifact.id)
    assert row is not None
    assert {f"run:{RUN_ID}".lower(), "node:n0000000001", "capture.shot"} <= set(row.tags)
    assert row.meta["run"] == RUN_ID and row.meta["op"] == "capture.shot"
    assert list((tmp_path / "port").iterdir()) == []


def test_checkpoint_shots_and_media_outputs(tmp_path: Path, store: DaemonArtifacts) -> None:
    port = spy_port(tmp_path)
    checkpoint = make_node("check.checkpoint", {"label": "liste"})
    call = make_call(checkpoint, store, port)
    outcome = CheckpointOutcome.model_validate(WorkerExecutor().run(call).value)
    assert outcome.label == "liste" and outcome.ok
    assert [check.kind for check in outcome.checks] == ["console_errors", "layout_spill"]
    assert len(outcome.shots) == 1 and outcome.shots == call.artifacts.produced
    shot = store.get(outcome.shots[0])
    assert shot is not None and shot.kind == "screenshot" and shot.name == "liste.png"

    without_shot = make_call(make_node("check.checkpoint", {"label": "sessiz", "shot": False}), store, port)
    assert CheckpointOutcome.model_validate(WorkerExecutor().run(without_shot).value).shots == []

    take = make_node("score.take", {"session": {"key": "take0", "app": "web-app", "platform": "web"},
                                    "size": [720, 1280]}, branches={"body": []})
    take_call = make_call(take, store, port)
    worker = WorkerExecutor()
    assert worker.enter(take_call).branches == ("body",)
    media = MediaInfo.model_validate(worker.exit(take_call, BodyOutcome("passed", ("body",))).value)
    assert media.kind == "video" and media.artifact is not None
    assert media.uri == f"artifact:{media.artifact}"
    assert (media.width, media.height) == (720, 1280)
    assert take_call.artifacts.produced == [media.artifact]


def test_media_value_files_and_kinds(tmp_path: Path, store: DaemonArtifacts) -> None:
    source = tmp_path / "clip.bin"
    source.write_bytes(b"clip")
    node = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}}, out="media")
    reply = DriverReply(value={"duration": "2s", "fps": 30, "uri": "file:ignored"},
                        files=(DriverFile(path=source, role="value", kind="audio", name="clip.wav"),))
    value = WorkerExecutor().run(make_call(node, store, ScriptedPort(reply))).value
    media = MediaInfo.model_validate(value)
    assert media.kind == "audio" and media.fps == 30 and media.duration is not None
    assert media.uri == f"artifact:{media.artifact}"

    other = tmp_path / "other.bin"
    other.write_bytes(b"other")
    unknown = DriverReply(value={"kind": "screencast"}, files=(DriverFile(path=other, role="value", kind="weird"),))
    with pytest.raises(DriverError) as caught:
        WorkerExecutor().run(make_call(node, store, ScriptedPort(unknown)))
    problem = caught.value
    assert problem.code == "driver_error"
    assert "names no media kind" in problem.message and "'screencast'" in problem.message
    assert "'weird'" in problem.message and node.id in problem.message
    assert "video, audio, image, file, bundle" in problem.message
    assert isinstance(problem.detail["artifact"], str)

    named = tmp_path / "named.bin"
    named.write_bytes(b"named")
    by_reply = DriverReply(value={"kind": "image"}, files=(DriverFile(path=named, role="value", kind="weird"),))
    assert MediaInfo.model_validate(WorkerExecutor().run(make_call(node, store, ScriptedPort(by_reply))).value).kind \
        == "image"


def test_evidence_files_are_only_recorded(tmp_path: Path, store: DaemonArtifacts) -> None:
    log = tmp_path / "steps.txt"
    log.write_text("tapped\n")
    reply = DriverReply(value={"x": 1}, files=(DriverFile(path=log, role="evidence", kind="log", caption="steps"),))
    node = make_node("ui.route")
    call = make_call(node, store, ScriptedPort(reply))
    assert WorkerExecutor().run(call).value == {"x": 1}
    [evidence] = call.artifacts.produced
    found = store.get(evidence)
    assert found is not None and found.name == "steps.txt" and found.kind == "log"


def test_protocol_errors_in_replies(tmp_path: Path, store: DaemonArtifacts) -> None:
    def file(name: str, role: FileRole) -> DriverFile:
        path = tmp_path / name
        path.write_bytes(name.encode())
        return DriverFile(path=path, role=role, kind="screenshot")

    shot_node = make_node("capture.shot", {"name": "a"})
    two = DriverReply(files=(file("a.png", "value"), file("b.png", "value")))
    with pytest.raises(DriverError) as caught:
        WorkerExecutor().run(make_call(shot_node, store, ScriptedPort(two)))
    assert caught.value.code == "driver_error" and "more than one value file" in caught.value.message

    tap = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}})
    with pytest.raises(DriverError) as wrong_out:
        WorkerExecutor().run(make_call(tap, store, ScriptedPort(DriverReply(files=(file("c.png", "value"),)))))
    assert wrong_out.value.code == "driver_error"

    with pytest.raises(DriverError) as no_object:
        WorkerExecutor().run(make_call(tap, store, ScriptedPort(DriverReply(files=(file("d.png", "shot"),)))))
    assert no_object.value.code == "driver_error" and "shots" in no_object.value.message

    gone = DriverReply(files=(DriverFile(path=tmp_path / "missing.png", role="evidence"),))
    with pytest.raises(DriverError) as missing:
        WorkerExecutor().run(make_call(tap, store, ScriptedPort(gone)))
    assert missing.value.code == "driver_error"

    media_node = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}}, out="media")
    invalid = DriverReply(value={"width": "wide"}, files=(file("e.bin", "value"),))
    with pytest.raises(DriverError) as bad_media:
        WorkerExecutor().run(make_call(media_node, store, ScriptedPort(invalid)))
    assert bad_media.value.code == "driver_error"


def test_capture_record_container(tmp_path: Path, store: DaemonArtifacts) -> None:
    port = spy_port(tmp_path)
    record = make_node("capture.record", {"name": "anlatim"}, branches={"body": []})
    call = make_call(record, store, port)
    worker = WorkerExecutor()
    entry = worker.enter(call)
    assert entry.branches == ("body",)
    assert entry.detail["fake"] is True
    result = worker.exit(call, BodyOutcome("passed", ("body",)))
    video = Artifact.model_validate(result.value)
    assert video.kind == "video" and video.name == "anlatim.json"
    assert result.detail["body"] == "passed"
    assert port.calls == [("capture.record", "enter"), ("capture.record", "exit")]
    assert [phase for _op, _args, _site, phase, _timeout in port.seen] == ["enter", "exit"]
    assert all(site == CallSite(record, "", 1) for _op, _args, site, _phase, _timeout in port.seen)


def test_port_failures(store: DaemonArtifacts) -> None:
    tap = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}})
    worker = WorkerExecutor()
    with pytest.raises(DriverError) as missing:
        worker.run(make_call(tap, store, None))
    assert missing.value.code == "driver_missing"
    assert "lease.web" in missing.value.message
    record = make_node("capture.record", {"name": "x"}, branches={"body": []})
    with pytest.raises(DriverError) as missing_container:
        worker.enter(make_call(record, store, None))
    assert missing_container.value.code == "driver_missing"

    with pytest.raises(DriverError) as broken:
        worker.run(make_call(tap, store, ScriptedPort(error=RuntimeError("socket closed"))))
    assert broken.value.code == "driver_error"
    assert "RuntimeError: socket closed" in broken.value.message
    assert broken.value.detail["type"] == "RuntimeError"
    assert isinstance(broken.value.__cause__, RuntimeError)

    original = DriverError("timeout", "element never appeared", retryable=True)
    with pytest.raises(DriverError) as passed:
        worker.run(make_call(tap, store, ScriptedPort(error=original)))
    assert passed.value is original

    soft = NodeFailure("check_failed", "layout spill", soft=True)
    with pytest.raises(NodeFailure) as kept:
        worker.exit(make_call(record, store, ScriptedPort(error=soft)), BodyOutcome("passed", ("body",)))
    assert kept.value is soft


def test_cancelled_calls_never_reach_the_port(tmp_path: Path, store: DaemonArtifacts) -> None:
    port = spy_port(tmp_path)
    tap = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}})
    call = make_call(tap, store, port)
    call.cancel.set()
    with pytest.raises(NodeFailure) as caught:
        WorkerExecutor().run(call)
    assert caught.value.code == "cancelled"
    assert port.seen == []
