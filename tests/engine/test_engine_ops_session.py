from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from eks_harness.engine.auth import AuthRequest, PersonaAuthRegistry
from eks_harness.engine.drivers import (
    CallPhase,
    CallSite,
    DriverFile,
    DriverReply,
    SnapshotKind,
)
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.executors import ArtifactSink, ExecContext, SessionState
from eks_harness.engine.model import (
    BackendHandle,
    BackendIdentity,
    FakeProfile,
    LeaseHandle,
    RunInfo,
    RunResources,
)
from eks_harness.engine.values import validate_args
from eks_harness.ir.plan import Node, Plan, Resource, SessionSpec
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.types import Artifact
from eks_harness.ops import SESSION_EXECUTOR
from eks_harness.ops.fakes import FakeDriverPort
from eks_harness.ops.session import AS_OP, SessionAsExecutor

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
LEASE_ID = "rlease01"
BACKEND_ID = "rback001"
TOKEN = "tok-admin-456"


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

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.append((type, dict(detail)))

    def progress(self, fraction: float, message: str) -> None:
        return None

    def artifact(self, artifact: Artifact) -> None:
        return None


class UnusedStore:
    def ingest(self, *args: Any, **kwargs: Any) -> Artifact:
        raise AssertionError("session.as ingests nothing in these tests")

    def get(self, artifact_id: str) -> Artifact | None:
        return None

    def file(self, artifact_id: str) -> None:
        return None

    def scratch(self) -> Any:
        raise AssertionError("session.as needs no scratch directory")


class RecordingPort:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, JsonValue], CallSite, CallPhase, float | None]] = []
        self.error = error

    @property
    def platform(self) -> str:
        return "web"

    @property
    def simulated(self) -> bool:
        return True

    def call(self, op: str, args: Mapping[str, JsonValue], *, site: CallSite, phase: CallPhase = "run",
             timeout: float | None = None) -> DriverReply:
        self.calls.append((op, dict(args), site, phase, timeout))
        if self.error is not None:
            raise self.error
        return DriverReply(detail={"logged_in": True})

    def snapshot(self, kinds: Sequence[SnapshotKind], dest: Path) -> list[DriverFile]:
        return []

    def version(self) -> str:
        return "recording-1"

    def close(self) -> None:
        return None


def handle() -> BackendHandle:
    identity = BackendIdentity(definition="testapi", backend_id="testapi@tree", lease_id="bl_1", source="request")
    secrets: dict[str, JsonValue] = {"url": "http://127.0.0.1:9/",
                                     "credentials": {"personas": {"harman-admin": {"token": TOKEN}}}}
    return BackendHandle(resource_id=BACKEND_ID, session_key="main", url="http://127.0.0.1:9/", identity=identity,
                         secrets=secrets)


def lease() -> LeaseHandle:
    return LeaseHandle(resource_id=LEASE_ID, kind="lease.web", platform="web", session_key="main", sid="sid001",
                       project="acme/session", session="session")


def make_call(persona: str, port: Any, *, uses: list[str] | None = None, state: SessionState | None = None,
              instance: str = "", attempt: int = 1) -> ExecContext:
    registry = default_registry()
    spec = registry.op(AS_OP)
    node = Node(id="n0000000001", op=AS_OP, args={"persona": persona}, out=spec.out, hash=HASH_A,
                uses=uses if uses is not None else [LEASE_ID, BACKEND_ID])
    resources = [
        Resource(id=LEASE_ID, kind="lease.web", app="web-app", session="main"),
        Resource(id=BACKEND_ID, kind="backend", app="web-app", session="main", definition="testapi"),
    ]
    plan = Plan(kind="flow", name="session", session=SessionSpec(app="web-app", platform="web", persona="viewer"),
                resources=resources, nodes=[node], hash=HASH_B)
    resolved = validate_args(spec, node.args)
    info = RunInfo(id="01JSESSIONRUN0000000000000", project="acme/session", session="session",
                   plan_hash=plan.hash, plan_kind=plan.kind, plan_name=plan.name, created_at=time.time())
    sink = RecordingSink()
    artifacts = ArtifactSink(UnusedStore(), run=info, node_id=node.id, instance=instance, op=AS_OP, sink=sink)
    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance=instance,
                       attempt=attempt, args=resolved.model, data=resolved.data, session=plan.session,
                       state=state if state is not None else SessionState("main", persona="viewer"),
                       resources=RunResources(leases={LEASE_ID: lease()}, backends={BACKEND_ID: handle()}),
                       driver=port, sink=sink, artifacts=artifacts, seed=0, clock=plan.clock,
                       deadline=time.monotonic() + 30, cancel=threading.Event())


def events_of(call: ExecContext) -> list[tuple[str, dict[str, JsonValue]]]:
    sink = call.sink
    assert isinstance(sink, RecordingSink)
    return sink.events


def test_spec() -> None:
    spec = SessionAsExecutor(PersonaAuthRegistry()).spec()
    assert spec.name == SESSION_EXECUTOR and spec.ops == (AS_OP,) and spec.run is not None
    assert spec.match(default_registry().op(AS_OP)) == 3


def test_session_as_with_credentials() -> None:
    port = RecordingPort()
    call = make_call("harman-admin", port, instance="n0000000005#0", attempt=2)
    result = SessionAsExecutor(PersonaAuthRegistry()).run(call)
    assert result.value is None
    assert result.detail == {"logged_in": True}
    assert call.state is not None and call.state.persona == "harman-admin"
    assert events_of(call) == [("node.persona", {"persona": "harman-admin", "api": True})]
    [(op, args, site, phase, timeout)] = port.calls
    assert op == AS_OP and phase == "run"
    assert args == {"persona": "harman-admin", "headers": {"Authorization": f"Bearer {TOKEN}"}}
    assert site == CallSite(call.node, "n0000000005#0", 2)
    assert timeout is not None and timeout > 0
    assert TOKEN not in repr(events_of(call))


def test_session_as_without_credentials() -> None:
    port = RecordingPort()
    call = make_call("muhasebeci", port)
    SessionAsExecutor(PersonaAuthRegistry()).run(call)
    assert call.state is not None and call.state.persona == "muhasebeci"
    assert events_of(call) == [("node.persona", {"persona": "muhasebeci", "api": False})]
    assert port.calls[0][1] == {"persona": "muhasebeci", "headers": {}}


def test_session_as_uses_registered_providers() -> None:
    class Provider:
        def headers(self, request: AuthRequest) -> Mapping[str, str] | None:
            return {"X-Persona": request.persona}

    auth = PersonaAuthRegistry()
    auth.register(Provider(), definition="testapi")
    port = RecordingPort()
    call = make_call("muhasebeci", port)
    SessionAsExecutor(auth).run(call)
    assert events_of(call)[0][1]["api"] is True
    assert port.calls[0][1]["headers"] == {"X-Persona": "muhasebeci"}


def test_session_as_without_a_backend_resource() -> None:
    port = RecordingPort()
    call = make_call("harman-admin", port, uses=[LEASE_ID])
    SessionAsExecutor(PersonaAuthRegistry()).run(call)
    assert events_of(call) == [("node.persona", {"persona": "harman-admin", "api": False})]
    assert port.calls[0][1] == {"persona": "harman-admin", "headers": {}}


def test_session_as_without_a_port() -> None:
    call = make_call("harman-admin", None)
    with pytest.raises(DriverError) as caught:
        SessionAsExecutor(PersonaAuthRegistry()).run(call)
    assert caught.value.code == "driver_missing"
    assert call.state is not None and call.state.persona == "harman-admin"


def test_session_as_port_errors() -> None:
    with pytest.raises(DriverError) as broken:
        SessionAsExecutor(PersonaAuthRegistry()).run(make_call("harman-admin", RecordingPort(OSError("gone"))))
    assert broken.value.code == "driver_error"
    login = DriverError("login_failed", "the login form rejected the persona")
    with pytest.raises(NodeFailure) as kept:
        SessionAsExecutor(PersonaAuthRegistry()).run(make_call("harman-admin", RecordingPort(login)))
    assert kept.value is login


def test_session_as_through_the_fake_port(tmp_path: Path) -> None:
    plan = Plan(kind="flow", name="session", session=SessionSpec(app="web-app", platform="web"), hash=HASH_B)
    info = RunInfo(id="01JSESSIONRUN0000000000000", project="acme/session", session="session",
                   plan_hash=plan.hash, plan_kind=plan.kind, plan_name=plan.name, created_at=time.time())
    port = FakeDriverPort(lease(), plan.session, info, FakeProfile(), default_registry(), tmp_path)
    call = make_call("harman-admin", port)
    assert SessionAsExecutor(PersonaAuthRegistry()).run(call).value is None
    assert port.calls == [(AS_OP, "run")]
