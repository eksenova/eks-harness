from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import JsonValue

import eks
from eks_harness.db import Database
from eks_harness.db.repos import projects, sessions
from eks_harness.engine.journal import (
    DETAIL_LIMIT_BYTES,
    Journal,
    RunJournal,
    bounded_detail,
)
from eks_harness.engine.model import (
    CacheCounts,
    EnvironmentOut,
    FakeProfile,
    NodeError,
    NodeEventIn,
    NodeEventRecord,
    NodeRecord,
    PauseInfo,
    PluginStamp,
    ResourceRecord,
    RunClock,
    RunInfo,
    RunOptions,
    RunQuery,
)
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry, default_registry

PROJECT = "acme/journal"
OTHER_PROJECT = "acme/other"
TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def make_plan(name: str = "journaled", path: str = "/api/branches") -> Plan:
    @eks.flow(app="web-app", platform="web", name=name)
    def flow(s: eks.Session) -> dict[str, Any]:
        s.record_op("ui.tap", TAP)
        s.record_op("api.get", {"path": path})
        info = s.record_op("api.get", {"path": "/api/branches"})
        return {"info": info}

    return eks.plan(flow)


def session_row(db: Database, project: str = PROJECT, name: str = "main") -> int:
    with db.transaction() as conn:
        projects.ensure(conn, project)
        session, _ = sessions.ensure_in_project(conn, project, name)
    return session.id


def make_info(run_id: str, plan: Plan, *, session_id: int | None, project: str = PROJECT, session: str = "main",
              created_at: float | None = None, **extra: Any) -> RunInfo:
    return RunInfo(id=run_id, project=project, session=session, session_id=session_id, plan_hash=plan.hash,
                   plan_kind=plan.kind, plan_name=plan.name, created_at=created_at or time.time(), **extra)


def create(journal: Journal, db: Database, run_id: str, plan: Plan | None = None, **kwargs: Any) -> RunInfo:
    chosen = plan or make_plan()
    project = kwargs.pop("project", PROJECT)
    session = kwargs.pop("session", "main")
    replay_of = kwargs.pop("replay_of", None)
    info = make_info(run_id, chosen, session_id=session_row(db, project, session), project=project,
                     session=session, **kwargs)
    journal.create_run(info, chosen, request={"tree": "/tmp/tree"}, environment=EnvironmentOut(),
                       simulated=False, replay_of=replay_of)
    return info


def node(run_id: str, node_id: str, seq: int, **fields: Any) -> NodeRecord:
    base: dict[str, Any] = {"run_id": run_id, "node_id": node_id, "seq": seq, "op": "ui.tap",
                            "node_hash": "sha256:" + "0" * 64, "status": "pending"}
    base.update(fields)
    return NodeRecord.model_validate(base)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def journaled(self, info: RunInfo, record: NodeEventRecord) -> None:
        self.calls.append(("journaled", record))

    def activity(self, info: RunInfo, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.calls.append(("activity", (type, dict(detail))))

    def progress(self, info: RunInfo, detail: Mapping[str, JsonValue]) -> None:
        self.calls.append(("progress", dict(detail)))

    def of(self, kind: str) -> list[Any]:
        return [value for name, value in self.calls if name == kind]


def test_migration_tables_and_status_check(db: Database) -> None:
    names = {row["name"] for row in db.conn().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"runs", "run_nodes", "node_events", "run_expansions", "run_resources", "cache_entries"} <= names
    journal = Journal(db)
    create(journal, db, "r-check")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn().execute("UPDATE runs SET status = 'exploded' WHERE id = 'r-check'")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn().execute(
            "INSERT INTO runs (id, plan_hash, plan_kind, plan_name, plan, status, created_at) "
            "VALUES ('bad', 'h', 'flow', 'n', '{}', 'sleeping', 1.0)")


def test_create_run_round_trip(db: Database) -> None:
    journal = Journal(db)
    plan = make_plan()
    session_id = session_row(db)
    info = make_info("r1", plan, session_id=session_id, tree="/tmp/tree", spec="flows/x.py", instance="cli-1",
                     actor="alice", options=RunOptions(debug=True, breakpoints=["op:ui.tap"]),
                     fakes=FakeProfile(families=["ui"]), clock=RunClock.model_validate({"cues": {"a": "1s"}}))
    environment = EnvironmentOut(harness="1.2.3", plugins={"tools": PluginStamp(version="1", digest="d")})
    row = journal.create_run(info, plan, request={"plan": {"kind": "flow"}, "tree": "/tmp/tree", "params": {"a": "1"}},
                             environment=environment, simulated=True, replay_of=None)
    assert row.status == "queued"
    assert row.reason is None
    assert row.info == info
    assert row.plan == plan
    assert row.plan.hash == plan.hash
    assert row.request == {"tree": "/tmp/tree", "params": {"a": "1"}}
    assert row.environment == environment
    assert row.simulated is True
    assert row.has_outputs is False
    assert row.outputs is None
    assert row.counts == {}
    assert row.cache == CacheCounts()
    assert row.started_at is None and row.finished_at is None
    stored = db.conn().execute("SELECT plan, request, simulated FROM runs WHERE id = 'r1'").fetchone()
    assert json.loads(stored["plan"]) == plan.model_dump(mode="json", by_alias=True)
    assert "plan" not in json.loads(stored["request"])
    assert stored["simulated"] == 1
    assert journal.run("missing") is None
    second = create(journal, db, "r2", replay_of="r1")
    replayed = journal.run(second.id)
    assert replayed is not None
    assert replayed.info.fakes is None and replayed.info.clock is None
    assert replayed.replay_of == "r1" and replayed.repeat_of is None


def test_set_status_outputs_counts_environment(db: Database) -> None:
    journal = Journal(db)
    create(journal, db, "r1")
    journal.set_status("r1", "preparing")
    row = journal.run("r1")
    assert row is not None and row.status == "preparing" and row.started_at is None
    journal.set_status("r1", "running", started_at=100.0)
    journal.set_status("r1", "paused", reason="breakpoint")
    journal.set_status("r1", "running", started_at=200.0)
    row = journal.run("r1")
    assert row is not None
    assert row.status == "running" and row.started_at == 100.0 and row.finished_at is None and row.reason is None
    journal.set_status("r1", "failed", reason="ui.tap n1 failed: boom", finished_at=300.0)
    row = journal.run("r1")
    assert row is not None
    assert row.status == "failed" and row.reason == "ui.tap n1 failed: boom" and row.finished_at == 300.0
    with pytest.raises(KeyError):
        journal.set_status("missing", "running")

    assert row.has_outputs is False
    journal.set_outputs("r1", None)
    row = journal.run("r1")
    assert row is not None and row.has_outputs is True and row.outputs is None
    journal.set_outputs("r1", {"branches": [1, 2]})
    row = journal.run("r1")
    assert row is not None and row.outputs == {"branches": [1, 2]}

    journal.set_counts("r1", {"passed": 3, "failed": 1}, CacheCounts(hits=2, stored=1))
    environment = EnvironmentOut(python="3.13", executors={"ui.tap": "fake@1"}, fakes=["ui.tap"])
    journal.set_environment("r1", environment)
    row = journal.run("r1")
    assert row is not None
    assert row.counts == {"passed": 3, "failed": 1}
    assert row.cache == CacheCounts(hits=2, stored=1)
    assert row.environment == environment


def test_upsert_node_and_json_fields(db: Database) -> None:
    journal = Journal(db)
    create(journal, db, "r1")
    journal.upsert_node(node("r1", "n2", 2, op="api.get"))
    journal.upsert_node(node("r1", "n1", 1))
    journal.upsert_node(node("r1", "n3", 0, instance="each1#0", parent_id="each1", parent_instance=""))
    first = journal.node("r1", "n1", "")
    assert first is not None and first.status == "pending" and first.has_output is False
    updated = node("r1", "n1", 1, status="failed", attempt=2, args={"target": {"by": "text"}},
                   output=None, has_output=True,
                   error=NodeError(code="assertion", message="nope", detail={"checkpoint": {"label": "x"}},
                                   soft=False),
                   cache="miss", cache_key="k1", resolution={"backend": {"commit": "abc"}},
                   executor="fake@1", location="worker", simulated=True, edited=True,
                   artifacts=["a1", "a2"], queued_at=1.0, started_at=2.0, finished_at=3.5, duration_ms=1500)
    journal.upsert_node(updated)
    assert journal.node("r1", "n1", "") == updated
    count = db.conn().execute("SELECT COUNT(*) AS n FROM run_nodes WHERE run_id = 'r1'").fetchone()["n"]
    assert count == 3
    assert [(record.node_id, record.instance) for record in journal.nodes("r1")] == [
        ("n3", "each1#0"), ("n1", ""), ("n2", "")]
    assert journal.node("r1", "n3", "each1#0") is not None
    assert journal.node("r1", "n3", "") is None
    valued = node("r1", "n2", 2, op="api.get", status="passed", output=[1, {"x": None}], has_output=True)
    journal.upsert_node(valued)
    assert journal.node("r1", "n2", "") == valued


def test_append_and_events(db: Database) -> None:
    journal = Journal(db)
    create(journal, db, "r1")
    create(journal, db, "r2")
    appended = [
        journal.append("r1", NodeEventIn(type="run.queued")),
        journal.append("r1", NodeEventIn(type="node.started", node_id="n1", attempt=1)),
        journal.append("r2", NodeEventIn(type="run.queued")),
        journal.append("r1", NodeEventIn(type="node.passed", node_id="n1", attempt=1, detail={"ms": 3})),
        journal.append("r1", NodeEventIn(type="node.started", node_id="n2", instance="e#1")),
    ]
    ids = [record.id for record in appended]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    own = journal.events("r1")
    assert [record.type for record in own] == ["run.queued", "node.started", "node.passed", "node.started"]
    assert own[2].detail == {"ms": 3} and own[2].attempt == 1
    assert own[3].instance == "e#1"
    assert own == [appended[0], appended[1], appended[3], appended[4]]
    assert [record.id for record in journal.events("r1", after=appended[1].id)] == [appended[3].id, appended[4].id]
    assert [record.type for record in journal.events("r1", node_id="n1")] == ["node.started", "node.passed"]
    assert len(journal.events("r1", limit=2)) == 2
    assert journal.event_count("r1") == 4
    assert journal.event_count("r2") == 1

    big = journal.append("r1", NodeEventIn(type="node.log", node_id="n1", detail={"text": "x" * 100 * 1024}))
    stored = journal.events("r1", after=appended[4].id)[0]
    assert stored == big
    assert stored.detail["truncated"] is True
    assert len(json.dumps(stored.detail, separators=(",", ":")).encode()) < 16 * 1024
    assert isinstance(stored.detail["text"], str) and stored.detail["text"].endswith("...")


def test_bounded_detail_never_raises() -> None:
    small = {"a": 1, "b": [1, 2], "c": None}
    assert bounded_detail(small) == small
    long_list = bounded_detail({"items": list(range(20_000))})
    assert long_list["truncated"] is True
    assert len(json.dumps(long_list, separators=(",", ":"))) <= DETAIL_LIMIT_BYTES
    wide: dict[str, JsonValue] = {f"key{index}": "v" * 50 for index in range(5_000)}
    bounded = bounded_detail(wide)
    assert bounded["truncated"] is True
    assert len(json.dumps(bounded, separators=(",", ":")).encode()) <= DETAIL_LIMIT_BYTES
    deep: Any = "leaf"
    for _ in range(5_000):
        deep = [deep]
    nested = bounded_detail({"deep": deep})
    assert nested["truncated"] is True
    assert len(json.dumps(nested, separators=(",", ":")).encode()) <= DETAIL_LIMIT_BYTES
    unicode = bounded_detail({"text": "ç" * 30_000}, limit=1024)
    assert unicode["truncated"] is True
    assert len(json.dumps(unicode, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    odd_input: dict[Any, Any] = {"nan": float("nan"), "tuple": (1, 2), 3: "x"}
    assert bounded_detail(odd_input) == {"nan": "nan", "tuple": [1, 2], "3": "x"}


def test_expansions_and_resources(db: Database) -> None:
    journal = Journal(db)
    create(journal, db, "r1")
    each = journal.record_expansion("r1", "each1", "", "each", {"items": [1, 2, 3]})
    branch = journal.record_expansion("r1", "when1", "each1#0", "branch", {"branch": "then"})
    assert journal.expansions("r1") == [each, branch]
    replaced = journal.record_expansion("r1", "each1", "", "each", {"items": [4]})
    assert {record.node_id: record.detail for record in journal.expansions("r1")} == {
        "each1": {"items": [4]}, "when1": {"branch": "then"}}
    assert replaced.detail == {"items": [4]}

    pending = ResourceRecord(run_id="r1", resource_id="lease.web:main", kind="lease.web", session_key="main",
                             state="acquiring")
    journal.set_resource(pending)
    assert journal.resources("r1") == [pending]
    held = pending.model_copy(update={"state": "held", "handle": "abc123", "detail": {"profile": "p1"},
                                      "acquired_at": 5.0})
    journal.set_resource(held)
    backend = ResourceRecord(run_id="r1", resource_id="backend:main", kind="backend", state="failed",
                             error="no backend", detail={"definition": "testapi"})
    journal.set_resource(backend)
    assert journal.resources("r1") == [held, backend]


def test_runs_filters_and_pagination(db: Database) -> None:
    journal = Journal(db)
    plan_a = make_plan("alpha")
    plan_b = make_plan("beta", "/api/other")
    assert plan_a.hash != plan_b.hash
    base = 1_000.0
    create(journal, db, "r1", plan_a, created_at=base + 1)
    create(journal, db, "r2", plan_b, created_at=base + 2, session="feature/x")
    create(journal, db, "r3", plan_a, created_at=base + 3, project=OTHER_PROJECT)
    create(journal, db, "r4", plan_b, created_at=base + 4)
    create(journal, db, "r5", plan_a, created_at=base + 4)
    journal.set_status("r2", "passed")
    journal.set_status("r4", "failed")

    def ids(query: RunQuery, **kwargs: Any) -> list[str]:
        rows, _cursor = journal.runs(query, **kwargs)
        return [row.info.id for row in rows]

    assert ids(RunQuery()) == ["r5", "r4", "r3", "r2", "r1"]
    assert ids(RunQuery(project=OTHER_PROJECT)) == ["r3"]
    assert ids(RunQuery(session="feature/x")) == ["r2"]
    assert ids(RunQuery(status=["passed", "failed"])) == ["r4", "r2"]
    assert ids(RunQuery(status=["queued"])) == ["r5", "r3", "r1"]
    assert ids(RunQuery(plan_hash=plan_a.hash)) == ["r5", "r3", "r1"]
    assert ids(RunQuery(name="beta")) == ["r4", "r2"]
    assert ids(RunQuery(name="beta", status=["failed"])) == ["r4"]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        rows, cursor = journal.runs(RunQuery(limit=2, cursor=cursor))
        pages += 1
        seen.extend(row.info.id for row in rows)
        if cursor is None:
            break
    assert seen == ["r5", "r4", "r3", "r2", "r1"]
    assert pages == 3
    with pytest.raises(ValueError, match="invalid run cursor"):
        journal.runs(RunQuery(cursor="not-a-cursor"))
    with pytest.raises(ValueError, match="invalid run cursor"):
        journal.runs(RunQuery(cursor="WzEsMiwzXQ"))

    other_session = journal.run("r3")
    feature = journal.run("r2")
    assert other_session is not None and feature is not None
    assert ids(RunQuery(), project_ids=[OTHER_PROJECT], session_ids=[]) == ["r3"]
    assert ids(RunQuery(), project_ids=[], session_ids=[feature.info.session_id or 0]) == ["r2"]
    assert ids(RunQuery(), project_ids=[OTHER_PROJECT], session_ids=[feature.info.session_id or 0]) == ["r3", "r2"]
    assert ids(RunQuery(), project_ids=[], session_ids=[]) == []
    assert ids(RunQuery(), project_ids=[OTHER_PROJECT]) == ["r3"]
    assert ids(RunQuery(), project_ids=None, session_ids=None) == ["r5", "r4", "r3", "r2", "r1"]


def test_unfinished_and_prune(db: Database) -> None:
    journal = Journal(db)
    for run_id in ("old-pass", "old-error", "new-pass", "active", "paused"):
        create(journal, db, run_id)
        journal.append(run_id, NodeEventIn(type="run.queued"))
        journal.upsert_node(node(run_id, "n1", 1))
        journal.record_expansion(run_id, "each1", "", "each", {"items": []})
        journal.set_resource(ResourceRecord(run_id=run_id, resource_id="slot", kind="slot", state="released"))
    journal.set_status("old-pass", "passed", finished_at=100.0)
    journal.set_status("old-error", "error", finished_at=150.0)
    journal.set_status("new-pass", "passed", finished_at=1_000.0)
    journal.set_status("active", "running")
    journal.set_status("paused", "paused")
    assert set(journal.unfinished()) == {"active", "paused"}
    assert journal.prune(500.0) == 2
    assert journal.run("old-pass") is None and journal.run("old-error") is None
    assert journal.run("new-pass") is not None and journal.run("active") is not None
    for table in ("run_nodes", "node_events", "run_expansions", "run_resources"):
        remaining = {row["run_id"] for row in db.conn().execute(f"SELECT DISTINCT run_id FROM {table}")}
        assert remaining == {"new-pass", "active", "paused"}
    assert journal.prune(500.0) == 0


def test_run_journal_publishes(db: Database) -> None:
    journal = Journal(db)
    info = create(journal, db, "r1")
    recorder = Recorder()
    writer = RunJournal(journal, recorder, info)
    writer.status("queued")
    writer.status("preparing")
    writer.status("running")
    writer.status("running")
    first = writer.event("node.started", node_id="n1", attempt=1, detail={"op": "ui.tap"})
    second = writer.event("node.passed", node_id="n1", attempt=1)
    writer.node(node("r1", "n1", 1, status="passed"))
    pause = PauseInfo(reason="breakpoint", node="n2", at=5.0)
    writer.status("paused", reason="breakpoint", pause=pause)
    writer.status("running")
    each = writer.expansion("each1", "", "each", {"items": [1]})
    writer.resource(ResourceRecord(run_id="r1", resource_id="slot", kind="slot", state="held", handle="t1"))
    writer.progress("n2", "", 1, 0.5, "half")
    writer.status("passed", reason=None)

    journaled = recorder.of("journaled")
    events = journal.events("r1")
    assert journaled == events
    assert [record.type for record in events] == [
        "run.queued", "run.preparing", "run.running", "run.running", "node.started", "node.passed", "run.paused",
        "run.running", "node.expanded", "resource.held", "run.passed"]
    assert first in journaled and second in journaled
    paused_event = events[6]
    assert paused_event.detail["reason"] == "breakpoint"
    assert paused_event.detail["pause"] == pause.model_dump(mode="json", by_alias=True)
    assert events[8].node_id == "each1" and events[8].detail == {"kind": "each", "detail": each.detail}
    assert events[9].node_id is None and events[9].detail["resourceId"] == "slot"
    assert "runId" not in events[9].detail
    assert [entry[0] for entry in recorder.of("activity")] == [
        "run.started", "run.paused", "run.resumed", "run.finished"]
    assert recorder.of("activity")[-1] == ("run.finished", {"status": "passed", "reason": None})
    assert recorder.of("progress") == [{"node": "n2", "instance": "", "attempt": 1, "fraction": 0.5,
                                        "message": "half"}]
    row = journal.run("r1")
    assert row is not None and row.status == "passed" and row.finished_at is not None
    assert journal.resources("r1")[0].handle == "t1"
    assert len(journal.expansions("r1")) == 1
    node_calls = [name for name, _value in recorder.calls]
    assert node_calls.count("journaled") == len(events)


def test_run_journal_is_thread_safe(db: Database) -> None:
    journal = Journal(db)
    info = create(journal, db, "r1")
    recorder = Recorder()
    writer = RunJournal(journal, recorder, info)
    start = threading.Barrier(8)

    def work(worker: int) -> int:
        start.wait()
        for index in range(50):
            writer.event("node.log", node_id=f"n{worker}", detail={"index": index})
        return worker

    with ThreadPoolExecutor(max_workers=8) as pool:
        finished = [future.result() for future in [pool.submit(work, worker) for worker in range(8)]]
    assert finished == list(range(8))
    events = journal.events("r1", limit=1000)
    ids = [record.id for record in events]
    assert len(ids) == 400 and len(set(ids)) == 400 and ids == sorted(ids)
    published = [record.id for record in recorder.of("journaled")]
    assert published == ids
    for worker in range(8):
        assert [record.detail["index"] for record in events if record.node_id == f"n{worker}"] == list(range(50))


def compact_utf8(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_lone_surrogates_never_break_journaling(db: Database) -> None:
    lone = "a\ud800b"
    detail: dict[str, JsonValue] = {"x": lone, "\udfff": ["😀\ud800"]}
    cleaned = bounded_detail(detail)
    assert cleaned == {"x": "a�b", "�": ["\U0001f600�"]}
    compact_utf8(cleaned)
    big = bounded_detail({"x": "\ud800" * 50_000})
    assert big["truncated"] is True
    assert len(compact_utf8(big)) <= DETAIL_LIMIT_BYTES
    tight = bounded_detail({"x": ["\udc00" * 10] * 10}, limit=16)
    assert tight["truncated"] is True
    compact_utf8(tight)

    journal = Journal(db)
    info = create(journal, db, "r-surrogate")
    record = journal.append(info.id, NodeEventIn(type="node.log", node_id="n1", detail=detail))
    assert record.detail == cleaned
    assert journal.events(info.id)[-1].detail == cleaned

    args: dict[str, JsonValue] = {"path": "/api/x", "body": {"name": lone}}
    error = NodeError(code="api_status", message=f"bad {lone}", detail={"body": lone})
    journal.upsert_node(node(info.id, "n1", 1, args=args, output=["\udc00"], has_output=True, error=error,
                             resolution={"digest": lone}))
    stored = journal.node(info.id, "n1", "")
    assert stored is not None
    assert stored.args == args and stored.output == ["\udc00"] and stored.resolution == {"digest": lone}
    assert stored.error == error

    journal.set_outputs(info.id, {"value": lone})
    journal.record_expansion(info.id, "each1", "", "each", {"items": [lone]})
    journal.set_resource(ResourceRecord(run_id=info.id, resource_id="b1", kind="backend", state="failed",
                                        error=f"boot {lone}"))
    journal.set_status(info.id, "failed", reason=f"broken {lone}")
    row = journal.run(info.id)
    assert row is not None
    assert row.outputs == {"value": lone} and row.reason == "broken a�b"
    assert journal.expansions(info.id)[0].detail == {"items": [lone]}
    assert journal.resources(info.id)[0].error == "boot a�b"

    other = create(journal, db, "r-surrogate-writer")
    recorder = Recorder()
    writer = RunJournal(journal, recorder, other)
    writer.status("running", reason=lone)
    writer.event("node.log", node_id="n1", detail={"line": lone})
    writer.resource(ResourceRecord(run_id=other.id, resource_id="b1", kind="backend", state="held", handle=lone,
                                   detail={"url": lone}))
    writer.progress("n1", "", 1, 0.5, lone)
    writer.status("error", reason=lone)
    for record in recorder.of("journaled"):
        compact_utf8(record.model_dump(mode="json", by_alias=True))
    assert recorder.of("activity")[-1] == ("run.finished", {"status": "error", "reason": "a�b"})
    assert recorder.of("progress")[0]["message"] == "a�b"


class SpyJournal(Journal):
    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.seen: list[tuple[str | None, int]] = []
        self.fail_on: str | None = None

    def append(self, run_id: str, event: NodeEventIn) -> NodeEventRecord:
        if event.type == self.fail_on:
            raise RuntimeError("append failed")
        observed: list[tuple[str | None, int]] = []

        def look() -> None:
            row = self.run(run_id)
            observed.append((None if row is None else row.status, self.event_count(run_id)))
            self.db.close_thread()

        reader = threading.Thread(target=look)
        reader.start()
        reader.join()
        self.seen.extend(observed)
        return super().append(run_id, event)


def test_status_and_its_event_commit_together(db: Database) -> None:
    journal = SpyJournal(db)
    info = create(journal, db, "r-atomic")
    recorder = Recorder()
    writer = RunJournal(journal, recorder, info)
    writer.status("running")
    assert journal.seen == [("queued", 0)]
    writer.status("passed")
    assert journal.seen[-1] == ("running", 1)
    row = journal.run(info.id)
    assert row is not None and row.status == "passed"
    assert [record.type for record in journal.events(info.id)] == ["run.running", "run.passed"]

    failing = create(journal, db, "r-rollback")
    rolled = Recorder()
    writer = RunJournal(journal, rolled, failing)
    writer.status("running")
    journal.fail_on = "run.failed"
    with pytest.raises(RuntimeError, match="append failed"):
        writer.status("failed", reason="boom")
    row = journal.run(failing.id)
    assert row is not None and row.status == "running" and row.finished_at is None and row.reason is None
    assert [record.type for record in journal.events(failing.id)] == ["run.running"]
    assert [record.type for record in rolled.of("journaled")] == ["run.running"]
    assert [entry[0] for entry in rolled.of("activity")] == ["run.started"]

    journal.fail_on = "node.expanded"
    with pytest.raises(RuntimeError, match="append failed"):
        writer.expansion("each1", "", "each", {"items": [1]})
    assert journal.expansions(failing.id) == []
    journal.fail_on = "resource.held"
    with pytest.raises(RuntimeError, match="append failed"):
        writer.resource(ResourceRecord(run_id=failing.id, resource_id="slot", kind="slot", state="held"))
    assert journal.resources(failing.id) == []


def test_run_journals_of_one_run_publish_in_id_order(db: Database) -> None:
    journal = Journal(db)
    info = create(journal, db, "r-shared")
    other = create(journal, db, "r-elsewhere")
    recorder = Recorder()
    writers = [RunJournal(journal, recorder, info), RunJournal(journal, recorder, info)]
    assert writers[0]._lock is writers[1]._lock
    assert RunJournal(journal, recorder, other)._lock is not writers[0]._lock
    start = threading.Barrier(8)

    def work(worker: int) -> None:
        start.wait()
        writer = writers[worker % 2]
        for index in range(25):
            if index % 10 == 0:
                writer.status("running")
            else:
                writer.event("node.log", node_id=f"n{worker}", detail={"index": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(work, worker) for worker in range(8)]:
            future.result()
    ids = [record.id for record in journal.events(info.id, limit=1000)]
    assert len(ids) == 200
    assert [record.id for record in recorder.of("journaled")] == ids
