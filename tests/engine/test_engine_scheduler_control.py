from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_engine_scheduler import TAP, Rig, lanes, no_overlap, register_probes, web_flow

import eks
from eks_harness.config import Config
from eks_harness.daemon.context import AppContext
from eks_harness.db import Database
from eks_harness.engine.model import (
    ControlRequest,
    ControlResponse,
    FakeAnswer,
    FakeProfile,
    RunOptions,
)
from eks_harness.engine.scheduler import SchedulerConfig
from eks_harness.ir.registry import Registry, default_registry


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        register_probes()
        yield chosen
    finally:
        chosen.restore(state)


@pytest.fixture
def make_rig(ctx: AppContext, db: Database, config: Config, tmp_path: Path) -> Callable[..., Rig]:
    def build(plan: Any, **kwargs: Any) -> Rig:
        return Rig(ctx=ctx, db=db, config=config, tmp_path=tmp_path, plan=plan, **kwargs)

    return build


def control(rig: Rig, action: str, **fields: Any) -> ControlResponse:
    found: list[ControlResponse] = []

    def send() -> None:
        found.append(rig.scheduler.control(ControlRequest.model_validate({"action": action, **fields}), timeout=10))

    worker = threading.Thread(target=send)
    worker.start()
    worker.join(15)
    assert found, f"{action} got no answer"
    return found[0]


def finish(rig: Rig, thread: threading.Thread) -> str:
    thread.join(15)
    assert rig.outcome is not None, "the run did not end"
    return rig.outcome.status


def wait_pause_at(rig: Rig, node_id: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pause = rig.scheduler.status().pause
        if pause is not None and pause.node == node_id:
            return pause
        time.sleep(0.01)
    raise AssertionError(f"the run did not pause at {node_id}: {rig.scheduler.status()}")


def test_node_breakpoint_pauses_before_the_node(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        first = s.record_op("probe.echo", {"value": "seen"})
        s.record_op("ui.tap", TAP)
        return first

    plan = web_flow(body)
    first, tap = plan.nodes
    rig = make_rig(plan, fakes=FakeProfile(families=["*"]), options=RunOptions(breakpoints=[tap.id]))
    thread = rig.start()
    pause = wait_pause_at(rig, tap.id)
    assert pause.reason == "breakpoint" and pause.breakpoint == "b1"
    assert isinstance(pause.args, dict) and pause.args["target"] == TAP["target"]
    assert pause.values == {first.id: "seen"}
    assert len(pause.snapshots) == 2
    assert {snapshot.kind for snapshot in pause.snapshots} == {"screenshot", "dom"}
    assert rig.scheduler.status().status == "paused"
    assert rig.node(tap.id).status == "paused"
    assert rig.journal.run(rig.run_id) is not None and rig.journal.run(rig.run_id).info.id == rig.run_id
    assert [event.type for event in rig.events() if event.type.startswith("run.")][-1] == "run.paused"
    assert rig.events("node.paused", tap.id)[0].detail["reason"] == "breakpoint"
    assert rig.node(tap.id).started_at is not None
    snapshot_ids = {snapshot.id for snapshot in pause.snapshots}
    assert snapshot_ids <= set(rig.node(tap.id).artifacts)
    answer = control(rig, "continue")
    assert answer.ok and answer.status == "running"
    assert finish(rig, thread) == "passed"
    assert rig.node(tap.id).status == "passed"
    assert [event.type for event in rig.events() if event.type in ("run.paused", "run.running")][-2:] == \
        ["run.paused", "run.running"]


def test_step_pause_labels_and_debug_ops(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.record_op("probe.echo", {"value": 1})
        s.record_op("probe.echo", {"value": 2})
        s.record_op("probe.echo", {"value": 3}, label="checkout")
        s.record_op("probe.echo", {"value": 4})

    plan = web_flow(body)
    one, two, three, four = plan.nodes
    rig = make_rig(plan, options=RunOptions(breakpoints=[one.id, "label:checkout"]))
    thread = rig.start()
    wait_pause_at(rig, one.id)
    assert control(rig, "step").ok
    pause = wait_pause_at(rig, two.id)
    assert pause.reason == "step"
    assert control(rig, "continue").ok
    pause = wait_pause_at(rig, three.id)
    assert pause.reason == "breakpoint" and pause.label == "checkout" and pause.breakpoint == "b2"
    assert control(rig, "continue").ok
    assert finish(rig, thread) == "passed"
    assert rig.node(four.id).status == "passed"

    def requested(s: eks.Session) -> Any:
        s.record_op("probe.echo", {"mode": "sleep", "seconds": 0.4})
        s.record_op("probe.echo", {"value": "next"})

    plan = web_flow(requested, name="requested")
    slow, nxt = plan.nodes
    rig = make_rig(plan, debug=True)
    thread = rig.start()
    deadline = time.monotonic() + 5
    while not rig.probe.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    answer = control(rig, "pause")
    assert answer.ok and "next node" in answer.message
    pause = wait_pause_at(rig, nxt.id)
    assert pause.reason == "pause"
    assert rig.node(slow.id).status == "passed"
    assert control(rig, "continue").ok
    assert finish(rig, thread) == "passed"

    def breaks(s: eks.Session) -> Any:
        flag = s.record_op("probe.echo", {"value": False})
        s.debug.break_("never", when=flag)
        s.debug.break_("always")
        s.debug.break_("labelled", when=flag)
        s.record_op("probe.echo", {"value": "done"})

    plan = web_flow(breaks, name="breaks")
    _flag, never, always, labelled, _done = plan.nodes
    rig = make_rig(plan, options=RunOptions(debug=True, breakpoints=["label:labelled"]))
    thread = rig.start()
    pause = wait_pause_at(rig, always.id)
    assert pause.reason == "source" and pause.label == "always"
    assert rig.node(never.id).status == "passed"
    assert control(rig, "continue").ok
    pause = wait_pause_at(rig, labelled.id)
    assert pause.reason == "breakpoint" and pause.label == "labelled"
    assert control(rig, "continue").ok
    assert finish(rig, thread) == "passed"
    assert rig.node(always.id).status == "passed" and rig.node(always.id).output is None

    def guarded(s: eks.Session) -> Any:
        s.debug.pause_on_error()
        s.record_op("probe.echo", {"mode": "fail", "code": "probe_failure"})

    plan = web_flow(guarded, name="guarded")
    _guard, failing = plan.nodes
    rig = make_rig(plan, debug=True)
    thread = rig.start()
    pause = wait_pause_at(rig, failing.id)
    assert pause.reason == "error" and pause.error is not None and pause.error.code == "probe_failure"
    assert control(rig, "continue").ok
    assert finish(rig, thread) == "failed"


def test_break_on_error_edit_retry_and_skip(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        broken = s.record_op("probe.echo", {"mode": "fail", "code": "probe_failure"})
        return s.record_op("probe.echo", {"value": broken})

    plan = web_flow(body)
    broken, _reader = plan.nodes
    rig = make_rig(plan, options=RunOptions(break_on_error=True))
    thread = rig.start()
    pause = wait_pause_at(rig, broken.id)
    assert pause.reason == "error" and pause.error is not None and pause.error.code == "probe_failure"
    assert rig.node(broken.id).status == "paused"
    edited = control(rig, "edit", args={"value": 5})
    assert edited.ok and edited.status == "paused"
    assert rig.events("node.args_edited", broken.id)[0].detail["args"] == {
        "value": 5, "mode": "echo", "seconds": 0.0, "code": "probe_failure", "retryable": False, "soft": False,
        "times": 0, "key": ""}
    invalid = control(rig, "edit", args={"seconds": "soon"})
    assert not invalid.ok and "seconds" in invalid.message
    assert control(rig, "retry").ok
    assert finish(rig, thread) == "passed"
    assert rig.outcome is not None and rig.outcome.outputs == 5
    record = rig.node(broken.id)
    assert record.status == "passed" and record.attempt == 2 and record.edited

    def counted(s: eks.Session) -> Any:
        total = s.record_op("ui.count", {"target": TAP["target"]})
        return s.record_op("probe.echo", {"value": total})

    plan = web_flow(counted, name="counted")
    count, echo = plan.nodes
    profile = FakeProfile(families=["*"], answers={count.id: FakeAnswer(fail="no rows", code="driver_error")})
    rig = make_rig(plan, fakes=profile, options=RunOptions(break_on_error=True))
    thread = rig.start()
    wait_pause_at(rig, count.id)
    refused = control(rig, "skip", value="three")
    assert not refused.ok and "expected int" in refused.message
    assert control(rig, "skip", value=3).ok
    assert finish(rig, thread) == "passed"
    assert rig.node(count.id).status == "skipped" and rig.node(count.id).output == 3
    assert rig.node(echo.id).output == 3

    rig = make_rig(plan, fakes=profile, options=RunOptions(break_on_error=True))
    thread = rig.start()
    wait_pause_at(rig, count.id)
    wrong = control(rig, "continue", node=echo.id)
    assert not wrong.ok and "paused at" in wrong.message
    assert control(rig, "continue").ok
    assert finish(rig, thread) == "failed"
    assert rig.outcome is not None and rig.outcome.reason == f"ui.count {count.id} failed: no rows"
    assert rig.node(count.id).status == "failed" and rig.node(echo.id).status == "cancelled"
    assert rig.events("node.failure_shot", count.id)


def test_do_runs_adhoc_ops(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.record_op("ui.tap", TAP)

    plan = web_flow(body)
    (tap,) = plan.nodes
    rig = make_rig(plan, fakes=FakeProfile(families=["*"]), options=RunOptions(breakpoints=[tap.id]))
    thread = rig.start()
    wait_pause_at(rig, tap.id)
    answer = control(rig, "do", op="probe.echo", args={"value": {"adhoc": True}})
    assert answer.ok and answer.result == {"adhoc": True} and answer.status == "paused"
    record = rig.node("adhoc:1")
    assert record.status == "passed" and record.output == {"adhoc": True} and record.instance == ""
    url = control(rig, "do", op="ui.url", args={})
    assert url.ok and url.result == "https://app.fake.local/"
    assert rig.node("adhoc:2").simulated
    unknown = control(rig, "do", op="probe.nothing", args={})
    assert not unknown.ok and "probe.nothing" in unknown.message
    invalid = control(rig, "do", op="probe.echo", args={"seconds": "never"})
    assert not invalid.ok
    failing = control(rig, "do", op="probe.echo", args={"mode": "fail", "code": "probe_failure"})
    assert not failing.ok and rig.node("adhoc:3").status == "failed"
    assert control(rig, "continue").ok
    assert finish(rig, thread) == "passed"


def test_abort_while_paused_and_pause_timeout(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.record_op("probe.echo", {"value": 1})

    plan = web_flow(body)
    (node,) = plan.nodes
    rig = make_rig(plan, options=RunOptions(breakpoints=[node.id]))
    thread = rig.start()
    wait_pause_at(rig, node.id)
    answer = control(rig, "abort")
    assert answer.ok
    assert finish(rig, thread) == "aborted"
    assert rig.outcome is not None and rig.outcome.reason == "aborted from the debugger"
    assert rig.node(node.id).status == "cancelled"

    rig = make_rig(plan, options=RunOptions(breakpoints=[node.id]),
                   settings=SchedulerConfig(parallelism=1, pause_timeout=0.3, cancel_grace=1.0))
    began = time.monotonic()
    outcome = rig.run()
    assert outcome.status == "aborted" and outcome.reason == "paused longer than 0.3 s"
    assert 0.3 <= time.monotonic() - began < 5
    late = rig.scheduler.control(ControlRequest(action="continue"), timeout=1)
    assert not late.ok


def test_debugging_off(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.debug.break_("ignored")
        s.record_op("probe.echo", {"mode": "sleep", "seconds": 0.3})

    plan = web_flow(body)
    brk, _slow = plan.nodes
    rig = make_rig(plan)
    thread = rig.start()
    deadline = time.monotonic() + 5
    while not rig.probe.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    for action in ("continue", "pause", "step"):
        answer = control(rig, action)
        assert not answer.ok and "debugging is off" in answer.message
    status = control(rig, "status")
    assert status.ok and status.debugger == "off"
    assert finish(rig, thread) == "passed"
    assert rig.node(brk.id).status == "passed"
    assert [event.detail for event in rig.events("node.passed", brk.id)] == [{"hit": True, "label": "ignored"}]


def test_eventually_rereads_idempotent_producers(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        total = s.record_op("probe.idem", {"mode": "count", "key": "rows"})
        s.record_op("expect.value", {"value": total, "assertion": {"kind": "ge", "value": 3}, "eventually": True,
                                     "timeout": "5s"})
        return s.record_op("probe.echo", {"value": total})

    plan = web_flow(body)
    producer, expectation, reader = plan.nodes
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "passed"
    assert outcome.outputs == 3
    rereads = rig.events("node.reread", producer.id)
    assert [event.detail["attempt"] for event in rereads] == [2, 3]
    assert all(event.detail["consumer"] == expectation.id for event in rereads)
    assert rig.node(producer.id).output == 3 and rig.node(producer.id).attempt == 3
    assert rig.node(expectation.id).output == {"ok": True, "value": 3}
    assert rig.node(reader.id).output == 3

    def plain(s: eks.Session) -> Any:
        total = s.record_op("probe.echo", {"mode": "count", "key": "plain"})
        s.record_op("expect.value", {"value": total, "assertion": {"kind": "ge", "value": 3}, "eventually": True,
                                     "timeout": "2s"})

    plan = web_flow(plain, name="plain")
    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "failed"
    record = rig.node(plan.nodes[1].id)
    assert record.error is not None and record.error.code == "assertion"
    assert "not idempotent" in record.error.message
    assert rig.events("node.reread") == []


def wait_for(check: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def test_do_waits_for_its_lease_and_takes_render_slots(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with eks.parallel():
            s.record_op("probe.lease", {"mode": "hold", "value": 1})
            s.record_op("probe.echo", {"value": 2})

    plan = web_flow(body)
    holder, echo = lanes(plan)
    rig = make_rig(plan, options=RunOptions(breakpoints=[echo.id]),
                   settings=SchedulerConfig(parallelism=2, cancel_grace=1.0))
    thread = rig.start()
    found: list[ControlResponse] = []
    try:
        wait_pause_at(rig, echo.id)
        wait_for(lambda: bool(rig.probe.ops("probe.lease")), "the holder to start")

        def send() -> None:
            found.append(rig.scheduler.control(
                ControlRequest(action="do", op="probe.lease", args={"value": "adhoc"}), timeout=10))

        sender = threading.Thread(target=send)
        sender.start()
        wait_for(lambda: rig.journal.node(rig.run_id, "adhoc:1", "") is not None, "the ad-hoc instance")
        time.sleep(0.3)
        assert not found
        assert len(rig.probe.ops("probe.lease")) == 1
        began = time.monotonic()
        status = control(rig, "status")
        assert status.ok and time.monotonic() - began < 1.0
        assert rig.scheduler.status().pause is not None
        assert rig.node("adhoc:1").status == "running"
    finally:
        rig.probe.release.set()
    sender.join(15)
    assert found and found[0].ok, found
    assert found[0].result == "adhoc" and found[0].status == "paused"
    assert rig.node(holder.id).status == "passed" and rig.node("adhoc:1").status == "passed"
    assert rig.probe.lease_peak == 1 and no_overlap(rig.probe.ops("probe.lease"))
    rendered = control(rig, "do", op="probe.render", args={"value": 5})
    assert rendered.ok and rendered.result == 5
    slots = [record for record in rig.journal.resources(rig.run_id)
             if record.kind == "slot" and record.detail.get("node") == "adhoc:2"]
    assert len(slots) == 1 and slots[0].state == "released" and slots[0].detail["location"] == "node"
    assert any(event.detail.get("kind") == "slot" for event in rig.events("resource.held"))
    assert control(rig, "continue").ok
    assert finish(rig, thread) == "passed"


def test_abort_while_a_do_runs(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        s.record_op("probe.echo", {"value": 1})

    plan = web_flow(body)
    (node,) = plan.nodes
    rig = make_rig(plan, options=RunOptions(breakpoints=[node.id]),
                   settings=SchedulerConfig(parallelism=1, cancel_grace=1.0))
    thread = rig.start()
    found: list[ControlResponse] = []
    try:
        wait_pause_at(rig, node.id)

        def send() -> None:
            found.append(rig.scheduler.control(
                ControlRequest(action="do", op="probe.echo", args={"mode": "hold"}), timeout=10))

        sender = threading.Thread(target=send)
        sender.start()
        wait_for(lambda: any(call.node == "adhoc:1" for call in rig.probe.calls), "the ad-hoc op to start")
        began = time.monotonic()
        rig.scheduler.abort("stop the ad-hoc op")
        assert time.monotonic() - began < 1.0
        thread.join(10)
        assert time.monotonic() - began < 3.0
        sender.join(10)
    finally:
        rig.probe.release.set()
    assert rig.outcome is not None and rig.outcome.status == "aborted"
    assert rig.outcome.reason == "stop the ad-hoc op"
    assert found and not found[0].ok
    assert rig.node("adhoc:1").status == "cancelled"
    assert rig.node(node.id).status == "cancelled"


def test_rereads_wait_for_the_producer_lease(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        with eks.parallel():
            total = s.record_op("probe.idlease", {"mode": "count", "key": "rows"})
            s.record_op("expect.value", {"value": total, "assertion": {"kind": "ge", "value": 3},
                                         "eventually": True, "timeout": "5s"})
            for index in range(3):
                s.record_op("probe.lease", {"mode": "sleep", "seconds": 0.2, "value": index})

    plan = web_flow(body)
    producer, expectation, *_sleepers = lanes(plan, {1: 0, 3: 2, 4: 3})
    rig = make_rig(plan, settings=SchedulerConfig(parallelism=4, cancel_grace=1.0))
    outcome = rig.run()
    assert outcome.status == "passed", outcome.reason
    rereads = rig.events("node.reread", producer.id)
    assert len(rereads) >= 2 and all(event.detail["consumer"] == expectation.id for event in rereads)
    assert rig.node(producer.id).output == 3
    assert rig.probe.lease_peak == 1
    assert no_overlap([call for call in rig.probe.calls if call.op in ("probe.lease", "probe.idlease")])
