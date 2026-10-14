from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest
from pydantic import BaseModel, JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import ExecContext, Reread, SessionState
from eks_harness.engine.model import RunInfo, RunResources
from eks_harness.engine.values import validate_args
from eks_harness.ir.ops.check import (
    Contains,
    Eq,
    ExpectValue,
    Falsy,
    Ge,
    Gt,
    Le,
    Length,
    Lt,
    Matches,
    Ne,
    Truthy,
)
from eks_harness.ir.plan import Node, Plan, SessionSpec
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.types import Artifact
from eks_harness.ops import EXPECT_EXECUTOR
from eks_harness.ops import expect as expect_module
from eks_harness.ops.expect import (
    DEFAULT_EVENTUALLY_SECONDS,
    VALUE_OP,
    ExpectValueExecutor,
    assertion_failure,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
SOURCE: dict[str, JsonValue] = {"$ref": "n0000000000", "path": ["total"]}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


@pytest.fixture
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(expect_module, "POLL_START_SECONDS", 0.02)
    monkeypatch.setattr(expect_module, "POLL_MAX_SECONDS", 0.08)


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
        raise AssertionError("expect.value ingests nothing")

    def get(self, artifact_id: str) -> Artifact | None:
        return None

    def file(self, artifact_id: str) -> None:
        return None

    def scratch(self) -> Any:
        raise AssertionError("expect.value needs no scratch directory")


def make_call(args: dict[str, JsonValue], resolved_value: JsonValue, *, reread: Reread | None = None,
              session: SessionSpec | None = None, deadline: float | None = None,
              cancel: threading.Event | None = None) -> ExecContext:
    from eks_harness.engine.executors import ArtifactSink

    registry = default_registry()
    spec = registry.op(VALUE_OP)
    node = Node(id="n0000000001", op=VALUE_OP, args={**args, "value": SOURCE}, out=spec.out, hash=HASH_A)
    plan = Plan(kind="flow", name="expect", session=session or SessionSpec(app="web-app", platform="web"),
                nodes=[node], hash=HASH_B)
    resolved = validate_args(spec, {**args, "value": resolved_value})
    info = RunInfo(id="01JEXPECTRUN00000000000000", project="acme/expect", session="expect", plan_hash=plan.hash,
                   plan_kind=plan.kind, plan_name=plan.name, created_at=time.time())
    sink = RecordingSink()
    artifacts = ArtifactSink(UnusedStore(), run=info, node_id=node.id, instance="", op=VALUE_OP, sink=sink)
    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance="", attempt=1,
                       args=resolved.model, data=resolved.data, session=plan.session, state=SessionState("main"),
                       resources=RunResources(), driver=None, sink=sink, artifacts=artifacts, seed=0,
                       clock=plan.clock, deadline=deadline, cancel=cancel or threading.Event(), reread=reread)


def sequence(values: list[JsonValue], calls: list[tuple[JsonValue, float]]) -> Callable[[JsonValue], JsonValue]:
    pending = list(values)

    def reread(expr: JsonValue) -> JsonValue:
        value = pending.pop(0) if len(pending) > 1 else pending[0]
        calls.append((expr, time.monotonic()))
        return value

    return reread


CASES: list[tuple[BaseModel, JsonValue, bool]] = [
    (Eq(value=1), 1, True),
    (Eq(value=1), 1.0, True),
    (Eq(value=1), True, False),
    (Eq(value=True), 1, False),
    (Eq(value={"a": [1, "x"]}), {"a": [1, "x"]}, True),
    (Eq(value={"a": [1, "x"]}), {"a": [1, "y"]}, False),
    (Eq(value=None), None, True),
    (Eq(value="1"), 1, False),
    (Ne(value=1), 2, True),
    (Ne(value=1), 1, False),
    (Ne(value=[1]), [True], True),
    (Contains(value="dün"), "merhaba dünya", True),
    (Contains(value="yok"), "merhaba dünya", False),
    (Contains(value=1), "abc1", False),
    (Contains(value={"id": 2}), [{"id": 1}, {"id": 2}], True),
    (Contains(value=3), [1, 2], False),
    (Contains(value=1), [True], False),
    (Contains(value="id"), {"id": 1}, True),
    (Contains(value="name"), {"id": 1}, False),
    (Contains(value=1), {"1": 1}, False),
    (Contains(value="a"), 5, False),
    (Matches(pattern=r"^Kad\w+"), "Kadıköy", True),
    (Matches(pattern=r"^\d+$"), "12a", False),
    (Matches(pattern=r"\d"), 5, False),
    (Matches(pattern=r"("), "x", False),
    (Gt(value=3), 4, True),
    (Gt(value=3), 3, False),
    (Gt(value=3), 3.5, True),
    (Gt(value=0), True, False),
    (Gt(value=3), "4", False),
    (Gt(value="3"), 4, False),
    (Ge(value=3), 3, True),
    (Ge(value=3), 2.99, False),
    (Lt(value=3), 2, True),
    (Lt(value=3), 3, False),
    (Le(value=3), 3, True),
    (Le(value=3), 3.01, False),
    (Le(value=3), None, False),
    (Truthy(), 1, True),
    (Truthy(), "x", True),
    (Truthy(), [], False),
    (Truthy(), 0, False),
    (Falsy(), None, True),
    (Falsy(), "", True),
    (Falsy(), {}, True),
    (Falsy(), {"a": 1}, False),
    (Length(equals=2), [1, 2], True),
    (Length(equals=2), "abc", False),
    (Length(at_least=1, at_most=2), {"a": 1}, True),
    (Length(at_least=2), {"a": 1}, False),
    (Length(at_most=2), "abc", False),
    (Length(at_least=0), 5, False),
]


@pytest.mark.parametrize(("assertion", "value", "holds"), CASES)
def test_assertion_failure(assertion: BaseModel, value: JsonValue, holds: bool) -> None:
    message = assertion_failure(assertion, value)
    if holds:
        assert message is None
    else:
        assert isinstance(message, str) and message


def test_assertion_messages_say_what_failed() -> None:
    assert assertion_failure(Eq(value=3), 2) == "expected 3, got 2"
    assert assertion_failure(Length(at_least=3), [1]) == "expected a length of at least 3, got 1"
    message = assertion_failure(Matches(pattern="("), "x")
    assert message is not None and "not a valid regular expression" in message
    long = assertion_failure(Eq(value=1), "z" * 1000)
    assert long is not None and len(long) < 300


def test_assertion_failure_dispatches_on_resolved_models() -> None:
    spec = default_registry().op(VALUE_OP)
    resolved = validate_args(spec, {"value": 5, "assertion": {"kind": "gt", "value": 3}})
    model = resolved.model
    assert isinstance(model, ExpectValue)
    assertion = model.assertion
    assert isinstance(assertion, Gt)
    assert assertion_failure(assertion, 5) is None
    assert assertion_failure(assertion, 2) is not None


def test_passing_value_outputs_ok() -> None:
    executor = ExpectValueExecutor()
    result = executor.run(make_call({"assertion": {"kind": "gt", "value": 0}}, 4))
    assert result.value == {"ok": True, "value": 4}
    spec = executor.spec()
    assert spec.name == EXPECT_EXECUTOR and spec.ops == (VALUE_OP,) and spec.run is not None


@pytest.mark.parametrize("soft", [False, True])
def test_failure_without_eventually(soft: bool) -> None:
    executor = ExpectValueExecutor()
    with pytest.raises(NodeFailure) as caught:
        executor.run(make_call({"assertion": {"kind": "eq", "value": "Moda"}, "soft": soft}, "Kadıköy"))
    problem = caught.value
    assert problem.code == "assertion"
    assert problem.soft is soft
    assert not problem.retryable
    assert problem.message == 'expected "Moda", got "Kadıköy"'
    assert problem.detail == {"value": "Kadıköy", "assertion": {"kind": "eq", "value": "Moda"}}


def test_failure_message_includes_the_authors_message() -> None:
    with pytest.raises(NodeFailure) as caught:
        ExpectValueExecutor().run(make_call({"assertion": {"kind": "truthy"}, "message": "list is empty"}, []))
    assert caught.value.message == "list is empty: expected a truthy value, got []"


@pytest.mark.usefixtures("fast_polls")
def test_eventually_rereads_with_backoff_until_it_holds() -> None:
    calls: list[tuple[JsonValue, float]] = []
    started = time.monotonic()
    call = make_call({"assertion": {"kind": "ge", "value": 3}, "eventually": True, "timeout": "5s"}, 0,
                     reread=sequence([1, 2, 3], calls))
    result = ExpectValueExecutor().run(call)
    assert result.value == {"ok": True, "value": 3}
    assert [expr for expr, _at in calls] == [SOURCE, SOURCE, SOURCE]
    assert calls[0][1] - started >= 0.02
    assert calls[1][1] - calls[0][1] >= 0.04
    assert calls[2][1] - calls[1][1] >= 0.08
    sink = call.sink
    assert isinstance(sink, RecordingSink)
    assert any(kind == "node.log" for kind, _detail in sink.events)


@pytest.mark.usefixtures("fast_polls")
def test_eventually_holding_at_once_needs_no_reread() -> None:
    calls: list[tuple[JsonValue, float]] = []
    call = make_call({"assertion": {"kind": "eq", "value": 1}, "eventually": True}, 1,
                     reread=sequence([2], calls))
    assert ExpectValueExecutor().run(call).value == {"ok": True, "value": 1}
    assert calls == []


@pytest.mark.usefixtures("fast_polls")
@pytest.mark.parametrize("soft", [False, True])
def test_eventually_fails_after_the_timeout(soft: bool) -> None:
    calls: list[tuple[JsonValue, float]] = []
    started = time.monotonic()
    call = make_call({"assertion": {"kind": "contains", "value": "Moda"}, "eventually": True, "timeout": "0.4s",
                      "soft": soft}, ["Kadıköy"], reread=sequence([["Beşiktaş"]], calls))
    with pytest.raises(NodeFailure) as caught:
        ExpectValueExecutor().run(call)
    elapsed = time.monotonic() - started
    assert 0.35 <= elapsed < 1.5
    problem = caught.value
    assert problem.code == "assertion" and problem.soft is soft
    assert "still after" in problem.message and "re-reads" in problem.message
    assert problem.detail["value"] == ["Beşiktaş"]
    assert len(calls) >= 3


@pytest.mark.usefixtures("fast_polls")
def test_eventually_uses_the_session_timeout() -> None:
    calls: list[tuple[JsonValue, float]] = []
    session = SessionSpec.model_validate({"app": "web-app", "platform": "web", "timeout": "0.3s"})
    started = time.monotonic()
    call = make_call({"assertion": {"kind": "truthy"}, "eventually": True}, 0, reread=sequence([0], calls),
                     session=session)
    with pytest.raises(NodeFailure):
        ExpectValueExecutor().run(call)
    assert 0.25 <= time.monotonic() - started < DEFAULT_EVENTUALLY_SECONDS


@pytest.mark.usefixtures("fast_polls")
def test_eventually_is_bounded_by_the_deadline() -> None:
    calls: list[tuple[JsonValue, float]] = []
    started = time.monotonic()
    call = make_call({"assertion": {"kind": "truthy"}, "eventually": True, "timeout": "30s"}, 0,
                     reread=sequence([0], calls), deadline=time.monotonic() + 0.3)
    with pytest.raises(NodeFailure):
        ExpectValueExecutor().run(call)
    assert time.monotonic() - started < 1.5


def test_eventually_without_a_reread_callable() -> None:
    call = make_call({"assertion": {"kind": "eq", "value": 2}, "eventually": True, "timeout": "1s",
                      "soft": True}, 1)
    with pytest.raises(NodeFailure) as caught:
        ExpectValueExecutor().run(call)
    problem = caught.value
    assert problem.code == "assertion" and problem.soft
    assert problem.message.startswith("expected 2, got 1; ")
    assert "re-read" in problem.message


def test_eventually_passes_other_reread_failures_through() -> None:
    def broken(expr: JsonValue) -> JsonValue:
        raise NodeFailure("http_error", "backend down", retryable=True)

    call = make_call({"assertion": {"kind": "eq", "value": 2}, "eventually": True, "timeout": "1s"}, 1,
                     reread=broken)
    with pytest.raises(NodeFailure) as caught:
        ExpectValueExecutor().run(call)
    assert caught.value.code == "http_error"


def test_eventually_stops_when_cancelled() -> None:
    cancel = threading.Event()
    calls: list[tuple[JsonValue, float]] = []
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    call = make_call({"assertion": {"kind": "truthy"}, "eventually": True, "timeout": "30s"}, 0,
                     reread=sequence([0], calls), cancel=cancel)
    with pytest.raises(NodeFailure) as caught:
        ExpectValueExecutor().run(call)
    assert caught.value.code == "cancelled"
    assert time.monotonic() - started < 3.0


def test_timeouts_must_be_seconds() -> None:
    call = make_call({"assertion": {"kind": "truthy"}, "eventually": True, "timeout": "8f"}, 0,
                     reread=sequence([0], []))
    with pytest.raises(NodeFailure) as caught:
        ExpectValueExecutor().run(call)
    assert caught.value.code == "args_invalid"
