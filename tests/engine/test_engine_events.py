from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from typing import Any, cast

import pytest

import eks
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.events import Event
from eks_harness.db.repos import projects, sessions
from eks_harness.engine.events import (
    ACTIVITY_TYPES,
    RUN_EVENT,
    RUN_PROGRESS,
    RUN_STARTED,
    RunEvents,
    format_run_sse,
    record_of,
    run_resource,
)
from eks_harness.engine.journal import Journal, RunJournal
from eks_harness.engine.model import (
    EnvironmentOut,
    NodeEventIn,
    NodeEventRecord,
    RunInfo,
)
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry, default_registry

PROJECT = "acme/events"
TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def make_plan() -> Plan:
    @eks.flow(app="web-app", platform="web")
    def streamed(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)

    return eks.plan(streamed)


def start_run(ctx: AppContext, run_id: str = "run-1") -> tuple[Journal, RunInfo]:
    with ctx.db.transaction() as conn:
        projects.ensure(conn, PROJECT)
        session, _ = sessions.ensure_in_project(conn, PROJECT, "main")
    plan = make_plan()
    info = RunInfo(id=run_id, project=PROJECT, session="main", session_id=session.id, plan_hash=plan.hash,
                   plan_kind=plan.kind, plan_name=plan.name, actor="alice", created_at=time.time())
    journal = Journal(ctx.db)
    journal.create_run(info, plan, request={}, environment=EnvironmentOut(), simulated=False)
    return journal, info


def parse(frame: bytes) -> dict[str, Any]:
    text = frame.decode("utf-8")
    assert text.endswith("\n\n")
    parsed: dict[str, Any] = {}
    for line in text.strip("\n").split("\n"):
        if line.startswith(":"):
            parsed["comment"] = line[1:].strip()
            continue
        key, _, value = line.partition(": ")
        parsed[key] = value
    return parsed


async def collect(stream: AsyncIterator[bytes], *, timeout: float = 10.0) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        return [parse(frame) async for frame in stream]

    return await asyncio.wait_for(run(), timeout)


def test_journaled_publishes_run_event(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    received: list[Event] = []
    remove = ctx.events.add_listener(received.append)
    try:
        record = journal.append(info.id, NodeEventIn(type="node.started", node_id="n1", attempt=1,
                                                   detail={"op": "ui.tap"}))
        events.journaled(info, record)
    finally:
        remove()
    (published,) = received
    assert published.type == RUN_EVENT
    assert published.persisted is False
    assert published.resource == run_resource(info.id) == f"run:{info.id}"
    assert published.project_id == PROJECT
    assert published.session_id == info.session_id
    assert published.detail == record.model_dump(mode="json", by_alias=True)
    assert published.detail["runId"] == info.id and published.detail["nodeId"] == "n1"
    assert record_of(published) == record
    assert ctx.events.history(types=[RUN_EVENT]) == []
    other = ctx.events.publish("lease.acquired", resource="browser:0", persist=False, detail={"id": 1})
    assert record_of(other) is None
    disguised = ctx.events.publish(RUN_EVENT, resource="lease:x", persist=False, detail=published.detail)
    assert record_of(disguised) is None
    broken = ctx.events.publish(RUN_EVENT, resource=run_resource(info.id), persist=False, detail={"id": "x"})
    assert record_of(broken) is None


def test_activity_is_persisted(ctx: AppContext) -> None:
    _journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    events.activity(info, RUN_STARTED, {"status": "running", "reason": None})
    (stored,) = ctx.events.history(types=[RUN_STARTED])
    assert stored.persisted is True
    assert stored.resource == run_resource(info.id)
    assert stored.project_id == PROJECT and stored.session_id == info.session_id
    assert stored.detail == {"run": info.id, "plan": {"kind": "flow", "name": info.plan_name}, "status": "running",
                             "reason": None}
    with pytest.raises(ValueError, match="not a run activity type"):
        events.activity(info, "run.exploded", {})
    assert "run.created" in ACTIVITY_TYPES


def test_progress_is_live_only(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    received: list[Event] = []
    remove = ctx.events.add_listener(received.append)
    try:
        writer = RunJournal(journal, events, info)
        writer.progress("n1", "", 1, 0.25, "loading")
    finally:
        remove()
    (published,) = received
    assert published.type == RUN_PROGRESS and published.persisted is False
    assert published.resource == run_resource(info.id)
    assert published.detail == {"node": "n1", "instance": "", "attempt": 1, "fraction": 0.25, "message": "loading"}
    assert journal.event_count(info.id) == 0
    assert ctx.events.history(types=[RUN_PROGRESS]) == []


def test_format_run_sse() -> None:
    node_event = NodeEventRecord(id=7, run_id="r1", type="node.passed", node_id="n1", instance="e#0", attempt=1,
                                 ts=12.5, detail={"ms": 3, "text": "ç"})
    frame = format_run_sse(node_event)
    assert frame.startswith(b"id: 7\nevent: node\ndata: ")
    assert frame.endswith(b"\n\n")
    data = json.loads(parse(frame)["data"])
    assert data == node_event.model_dump(mode="json", by_alias=True)
    assert data["nodeId"] == "n1" and data["runId"] == "r1"
    assert "ç" in frame.decode("utf-8")
    run_event = NodeEventRecord(id=8, run_id="r1", type="run.passed", ts=13.0, detail={"reason": None})
    assert parse(format_run_sse(run_event)) == {
        "id": "8", "event": "run",
        "data": json.dumps(run_event.model_dump(mode="json", by_alias=True), separators=(",", ":"))}
    for type, prefix in (("resource.held", "resource"), ("break.added", "break"), ("control", "control")):
        record = NodeEventRecord(id=9, run_id="r1", type=type, ts=1.0)
        assert parse(format_run_sse(record))["event"] == prefix


def test_replay_of_finished_run(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    writer = RunJournal(journal, RunEvents(ctx.events), info)
    writer.status("preparing")
    writer.status("running")
    writer.status("passed")
    stored = journal.events(info.id)
    events = RunEvents(ctx.events)

    frames = asyncio.run(collect(events.stream(journal, info.id)))
    assert frames[0] == {"retry": "3000"}
    assert [frame["id"] for frame in frames[1:4]] == [str(record.id) for record in stored]
    assert [json.loads(frame["data"])["type"] for frame in frames[1:4]] == ["run.preparing", "run.running",
                                                                           "run.passed"]
    assert frames[-1] == {"event": "end", "data": "passed"}
    assert len(frames) == 5

    resumed = asyncio.run(collect(events.stream(journal, info.id, last_event_id=stored[0].id,
                                                finished=lambda: True)))
    assert [frame.get("id") for frame in resumed] == [None, str(stored[1].id), str(stored[2].id), None]
    assert resumed[-1] == {"event": "end", "data": "passed"}
    assert ctx.events.subscriber_count == 0


def test_replay_pages_through_the_journal(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    import eks_harness.engine.events as events_module

    monkeypatch.setattr(events_module, "REPLAY_PAGE", 2)
    journal, info = start_run(ctx)
    writer = RunJournal(journal, RunEvents(ctx.events), info)
    for index in range(5):
        writer.event("node.log", node_id="n1", detail={"index": index})
    writer.status("passed")
    frames = asyncio.run(collect(RunEvents(ctx.events).stream(journal, info.id)))
    ids = [int(frame["id"]) for frame in frames if "id" in frame]
    assert ids == [record.id for record in journal.events(info.id)]
    assert len(ids) == 6
    assert frames[-1] == {"event": "end", "data": "passed"}


def test_live_stream_without_duplicates(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    writer = RunJournal(journal, events, info)
    writer.status("preparing")
    writer.status("running")
    done = False

    async def scenario() -> list[dict[str, Any]]:
        stream = events.stream(journal, info.id, finished=lambda: done, heartbeat=30.0)
        frames = [parse(await anext(stream))]
        raced = writer.event("node.started", node_id="n1", attempt=1)
        frames.append(parse(await anext(stream)))
        frames.append(parse(await anext(stream)))
        frames.append(parse(await anext(stream)))
        replayed = journal.events(info.id)
        assert [frame["id"] for frame in frames[1:]] == [str(record.id) for record in replayed]
        assert replayed[-1] == raced

        async def rest() -> list[dict[str, Any]]:
            return [parse(frame) async for frame in stream]

        task = asyncio.create_task(rest())
        await asyncio.sleep(0.05)
        await asyncio.to_thread(writer.event, "node.passed", node_id="n1", attempt=1)
        await asyncio.to_thread(writer.progress, "n2", "", 1, 0.5, "half")
        await asyncio.to_thread(writer.event, "node.started", node_id="n2", attempt=1)
        await asyncio.sleep(0.05)
        nonlocal done
        await asyncio.to_thread(writer.status, "passed")
        done = True
        frames.extend(await asyncio.wait_for(task, 10.0))
        return frames

    frames = asyncio.run(scenario())
    stored = journal.events(info.id)
    with_ids = [frame["id"] for frame in frames if "id" in frame]
    assert with_ids == [str(record.id) for record in stored]
    assert len(with_ids) == len(set(with_ids))
    progress = [frame for frame in frames if frame.get("event") == "progress"]
    assert progress == [{"event": "progress", "data": json.dumps(
        {"node": "n2", "instance": "", "attempt": 1, "fraction": 0.5, "message": "half"}, separators=(",", ":"))}]
    progress_index = frames.index(progress[0])
    passed_index = next(index for index, frame in enumerate(frames)
                        if "data" in frame and frame.get("event") == "node"
                        and json.loads(frame["data"])["type"] == "node.passed")
    assert passed_index < progress_index
    assert frames[-1] == {"event": "end", "data": "passed"}
    assert ctx.events.subscriber_count == 0


def test_heartbeat_and_disconnect(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)

    async def heartbeat() -> list[dict[str, Any]]:
        stream = events.stream(journal, info.id, heartbeat=0.05, finished=lambda: False)
        first = parse(await anext(stream))
        second = parse(await asyncio.wait_for(anext(stream), 5.0))
        third = parse(await asyncio.wait_for(anext(stream), 5.0))
        await cast("AsyncGenerator[bytes]", stream).aclose()
        return [first, second, third]

    assert asyncio.run(heartbeat()) == [{"retry": "3000"}, {"comment": "keep-alive"}, {"comment": "keep-alive"}]
    assert ctx.events.subscriber_count == 0

    flag = {"gone": False}

    def sync_check() -> bool:
        return flag["gone"]

    async def disconnect_sync() -> list[dict[str, Any]]:
        stream = events.stream(journal, info.id, heartbeat=0.05, is_disconnected=sync_check,
                               finished=lambda: False)
        frames = [parse(await anext(stream))]
        flag["gone"] = True
        frames.extend(await collect(stream, timeout=5.0))
        return frames

    assert asyncio.run(disconnect_sync()) == [{"retry": "3000"}]

    async def async_check() -> bool:
        return True

    frames = asyncio.run(collect(events.stream(journal, info.id, is_disconnected=async_check,
                                               finished=lambda: False), timeout=5.0))
    assert frames == [{"retry": "3000"}]
    assert ctx.events.subscriber_count == 0


def test_overflow_ends_the_stream(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    writer = RunJournal(journal, events, info)

    async def scenario() -> list[dict[str, Any]]:
        stream = events.stream(journal, info.id, finished=lambda: False)
        frames = [parse(await anext(stream))]
        for index in range(1_100):
            writer.progress("n1", "", 1, index / 1_100, "")
        frames.extend(await collect(stream, timeout=5.0))
        return frames

    assert asyncio.run(scenario()) == [{"retry": "3000"}]
    assert ctx.events.subscriber_count == 0


def test_stream_waits_when_finished_runs_ahead_of_the_journal(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    writer = RunJournal(journal, events, info)
    writer.status("running")

    async def scenario() -> tuple[bool, list[dict[str, Any]]]:
        stream = events.stream(journal, info.id, finished=lambda: True, heartbeat=30.0)
        task = asyncio.create_task(collect(stream, timeout=10.0))
        await asyncio.sleep(0.4)
        waiting = not task.done()
        await asyncio.to_thread(writer.status, "passed")
        return waiting, await task

    waiting, frames = asyncio.run(scenario())
    assert waiting
    assert [json.loads(frame["data"])["type"] for frame in frames if "id" in frame] == ["run.running", "run.passed"]
    assert frames[-1] == {"event": "end", "data": "passed"}
    assert ctx.events.subscriber_count == 0


def test_stream_without_finished_follows_the_live_terminal_event(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    writer = RunJournal(journal, events, info)
    writer.status("running")

    async def scenario() -> list[dict[str, Any]]:
        task = asyncio.create_task(collect(events.stream(journal, info.id, heartbeat=30.0), timeout=10.0))
        await asyncio.sleep(0.1)
        assert not task.done()
        await asyncio.to_thread(writer.event, "node.started", node_id="n1", attempt=1)
        await asyncio.to_thread(writer.status, "failed", reason="boom")
        return await task

    frames = asyncio.run(scenario())
    assert [json.loads(frame["data"])["type"] for frame in frames if "id" in frame] == [
        "run.running", "node.started", "run.failed"]
    assert frames[-1] == {"event": "end", "data": "failed"}
    assert ctx.events.subscriber_count == 0


def test_stream_of_a_status_written_without_its_event_ends(ctx: AppContext) -> None:
    journal, info = start_run(ctx)
    journal.set_status(info.id, "error", reason="recovered")
    frames = asyncio.run(collect(RunEvents(ctx.events).stream(journal, info.id), timeout=5.0))
    assert frames == [{"retry": "3000"}, {"event": "end", "data": "error"}]


def test_frames_with_lone_surrogates_stay_utf8(ctx: AppContext) -> None:
    record = NodeEventRecord(id=3, run_id="r1", type="node.log", node_id="n1", ts=1.0, detail={"line": "a\ud800"})
    frame = format_run_sse(record)
    assert json.loads(parse(frame)["data"])["detail"] == {"line": "a\ud800"}
    journal, info = start_run(ctx)
    events = RunEvents(ctx.events)
    writer = RunJournal(journal, events, info)

    async def scenario() -> list[dict[str, Any]]:
        stream = events.stream(journal, info.id, finished=lambda: False, heartbeat=30.0)
        frames = [parse(await anext(stream))]
        events.progress(info, {"message": "b\udc00"})
        frames.append(parse(await asyncio.wait_for(anext(stream), 5.0)))
        await asyncio.to_thread(writer.event, "node.log", node_id="n1", detail={"line": "c\ud800"})
        frames.append(parse(await asyncio.wait_for(anext(stream), 5.0)))
        await cast("AsyncGenerator[bytes]", stream).aclose()
        return frames

    frames = asyncio.run(scenario())
    assert json.loads(frames[1]["data"]) == {"message": "b\udc00"}
    assert json.loads(frames[2]["data"])["detail"] == {"line": "c�"}
    assert ctx.events.subscriber_count == 0
