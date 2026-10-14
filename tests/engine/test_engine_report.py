from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

import eks
from eks_harness.api.links import Links
from eks_harness.config import Config
from eks_harness.db import Database
from eks_harness.db.repos import projects, sessions
from eks_harness.engine.errors import RunNotFound
from eks_harness.engine.journal import Journal
from eks_harness.engine.model import (
    ArtifactRef,
    CacheCounts,
    CheckOutcome,
    CheckpointOutcome,
    EnvironmentOut,
    IssueOut,
    NodeError,
    NodeEventIn,
    NodeRecord,
    PauseInfo,
    ResourceRecord,
    RunInfo,
)
from eks_harness.engine.report import (
    API_PREFIX,
    SLOWEST_NODES,
    artifact_ref,
    build_report,
    compare_runs,
    journal_page,
    run_links,
    summary,
)
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.types import Artifact

PROJECT = "acme/report"
HASH = "sha256:" + "0" * 64
TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class FakeLinks:
    def absolute(self, path: str) -> str:
        return "http://hub" + path

    def session(self, project_id: str, slug: str | None) -> str:
        return f"http://hub/p/{project_id}" + (f"/s/{slug}" if slug else "")


class Store:
    def __init__(self, *ids: str) -> None:
        self.items = {artifact_id: Artifact(id=artifact_id, kind="screenshot", name=f"{artifact_id}.png",
                                            url=f"http://hub/a/{artifact_id}", raw_url=f"http://hub/raw/{artifact_id}",
                                            sha256="sha256:" + artifact_id)
                      for artifact_id in ids}
        self.asked: list[str] = []

    def get(self, artifact_id: str) -> Artifact | None:
        self.asked.append(artifact_id)
        return self.items.get(artifact_id)


def make_plan() -> Plan:
    @eks.flow(app="web-app", platform="web")
    def reported(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)

    return eks.plan(reported)


def start_run(db: Database, run_id: str, *, created_at: float | None = None) -> tuple[Journal, RunInfo]:
    with db.transaction() as conn:
        projects.ensure(conn, PROJECT)
        session, _ = sessions.ensure_in_project(conn, PROJECT, "feature/report")
    plan = make_plan()
    info = RunInfo(id=run_id, project=PROJECT, session="feature/report", session_id=session.id, tree="/tmp/tree",
                   spec="flows/x.py", plan_hash=plan.hash, plan_kind=plan.kind, plan_name=plan.name, actor="alice",
                   instance="cli", created_at=created_at if created_at is not None else time.time())
    journal = Journal(db)
    journal.create_run(info, plan, request={}, environment=EnvironmentOut(), simulated=True, repeat_of=None)
    return journal, info


def record(run_id: str, node_id: str, seq: int, **fields: Any) -> NodeRecord:
    base: dict[str, Any] = {"run_id": run_id, "node_id": node_id, "seq": seq, "op": "ui.tap", "node_hash": HASH,
                            "status": "passed"}
    base.update(fields)
    return NodeRecord.model_validate(base)


def set_ts(db: Database, event_id: int, ts: float) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE node_events SET ts = ? WHERE id = ?", (ts, event_id))


def test_summary_and_links(db: Database, config: Config) -> None:
    journal, info = start_run(db, "r1")
    links = run_links("r1", FakeLinks(), project=PROJECT, session_slug="feature-report")
    assert links.run == f"http://hub{API_PREFIX}/r1"
    assert links.report == "http://hub/api/runs/r1/report"
    assert links.journal == "http://hub/api/runs/r1/journal"
    assert links.events == "http://hub/api/runs/r1/events"
    assert links.plan == "http://hub/api/runs/r1/plan"
    assert links.session == "http://hub/p/acme/report/s/feature-report"
    assert run_links("r1", FakeLinks()).session is None
    real = run_links("r1", Links(config), project=PROJECT, session_slug="feature-report")
    assert real.run == Links(config).absolute("/api/runs/r1")
    assert real.session == Links(config).session(PROJECT, "feature-report")

    journal.set_status("r1", "running", started_at=info.created_at + 1)
    journal.set_counts("r1", {"passed": 2}, CacheCounts(hits=1))
    row = journal.run("r1")
    assert row is not None
    pause = PauseInfo(reason="step", node="n1", at=5.0)
    out = summary(row, FakeLinks(), pause=pause, session_slug="feature-report")
    assert out.id == "r1" and out.status == "running"
    assert out.plan.kind == "flow" and out.plan.name == info.plan_name and out.plan.hash == info.plan_hash
    assert out.project == PROJECT and out.session == "feature/report"
    assert out.tree == "/tmp/tree" and out.spec == "flows/x.py" and out.actor == "alice" and out.instance == "cli"
    assert out.created_at == info.created_at and out.started_at == info.created_at + 1 and out.finished_at is None
    assert out.counts == {"passed": 2} and out.cache == CacheCounts(hits=1)
    assert out.pause == pause
    assert out.simulated is True
    assert out.links == links

    artifact = Artifact(id="a1", kind="video", name="clip.mp4", url="u", raw_url="r", sha256="s")
    assert artifact_ref(artifact, node="n1", instance="e#0") == ArtifactRef(
        id="a1", kind="video", name="clip.mp4", url="u", raw_url="r", node="n1", instance="e#0")


def test_build_report(db: Database) -> None:
    journal, _info = start_run(db, "r1")
    ok_check = CheckOutcome(kind="layout", ok=True, message="fine")
    bad_check = CheckOutcome(kind="errors", ok=False, message="console error", detail={"count": 2})
    warn_check = CheckOutcome(kind="spill", ok=False, severity="warning", message="spill")
    passed_cp = CheckpointOutcome(label="list", viewport="1280x800", ok=True, checks=[ok_check], shots=["a2"])
    failed_cp = CheckpointOutcome(label="form", ok=False, checks=[ok_check, bad_check, warn_check], shots=["a3"])
    nodes = [
        record("r1", "n1", 1, artifacts=["a1"], duration_ms=100, cache="miss"),
        record("r1", "cp1", 2, op="check.checkpoint", output=passed_cp.model_dump(mode="json", by_alias=True),
               has_output=True, artifacts=["a2"], duration_ms=300),
        record("r1", "cp2", 3, op="check.checkpoint", status="failed",
               error=NodeError(code="check_failed", message="checkpoint form failed",
                               detail={"checkpoint": failed_cp.model_dump(mode="json", by_alias=True)}),
               artifacts=["a3", "a4"], duration_ms=50),
        record("r1", "n4", 4, op="api.get", status="soft_failed", instance="each#1", label="probe", src="f.py:9",
               error=NodeError(code="http_status", message="404", soft=True, detail={"status": 404}),
               artifacts=["gone"], duration_ms=20),
        record("r1", "n5", 5, status="cancelled"),
    ]
    for node in reversed(nodes):
        journal.upsert_node(node)
    journal.append("r1", NodeEventIn(type="run.warning", detail=IssueOut(
        severity="warning", code="plan.slow", message="slow op", node="n1").model_dump(mode="json", by_alias=True)))
    journal.append("r1", NodeEventIn(type="run.warning", detail={"message": "odd warning", "extra": 1}))
    journal.append("r1", NodeEventIn(type="node.failure_shot", node_id="cp2", attempt=1, detail={"artifacts": ["a4"]}))
    journal.append("r1", NodeEventIn(type="node.failure_shot", node_id="n1", detail={"artifacts": "bad"}))
    resource = ResourceRecord(run_id="r1", resource_id="lease.web:main", kind="lease.web", state="released",
                              handle="sid1", acquired_at=1.0, released_at=2.0)
    journal.set_resource(resource)
    environment = EnvironmentOut(harness="1.0", fakes=["ui.tap"])
    journal.set_environment("r1", environment)
    journal.set_outputs("r1", {"branches": ["a", "b"]})
    journal.set_counts("r1", {"passed": 2, "failed": 1, "soft_failed": 1, "cancelled": 1},
                       CacheCounts(misses=1, stored=1))
    journal.set_status("r1", "failed", reason="check.checkpoint cp2 failed: checkpoint form failed")
    store = Store("a1", "a2", "a3", "a4")

    report = build_report(journal, "r1", FakeLinks(), store, session_slug="feature-report")
    assert report.run.status == "failed"
    assert report.run.links.session == "http://hub/p/acme/report/s/feature-report"

    hard, soft = report.failures
    assert hard.node == "cp2" and hard.op == "check.checkpoint" and hard.code == "check_failed"
    assert hard.soft is False and hard.message == "checkpoint form failed"
    assert hard.checks == [bad_check, warn_check]
    assert [shot.id for shot in hard.shots] == ["a4", "a3"]
    assert all(shot.node == "cp2" for shot in hard.shots)
    assert hard.detail["checkpoint"] == failed_cp.model_dump(mode="json", by_alias=True)
    assert soft.node == "n4" and soft.instance == "each#1" and soft.soft is True and soft.code == "http_status"
    assert soft.label == "probe" and soft.src == "f.py:9" and soft.shots == [] and soft.detail == {"status": 404}

    first, second = report.checkpoints
    assert first.node == "cp1" and first.label == "list" and first.ok is True and first.viewport == "1280x800"
    assert first.checks == [ok_check] and [shot.id for shot in first.shots] == ["a2"]
    assert second.node == "cp2" and second.ok is False and second.checks == failed_cp.checks
    assert [shot.id for shot in second.shots] == ["a3"]

    assert [(ref.id, ref.node) for ref in report.artifacts] == [("a1", "n1"), ("a2", "cp1"), ("a3", "cp2"),
                                                                ("a4", "cp2")]
    assert report.artifacts[0].url == "http://hub/a/a1" and report.artifacts[0].kind == "screenshot"
    assert store.asked.count("a4") == 1
    assert report.values == {"branches": ["a", "b"]}
    assert report.cache == CacheCounts(misses=1, stored=1)
    assert report.resources == [resource]
    assert report.environment == environment
    assert report.issues[0] == IssueOut(severity="warning", code="plan.slow", message="slow op", node="n1")
    assert report.issues[1].message == "odd warning" and report.issues[1].severity == "warning"
    assert len(report.issues) == 2
    assert report.journal.run_id == "r1" and report.journal.nodes == 5
    assert report.journal.events == journal.event_count("r1") == 4
    assert [timing.node for timing in report.timings.nodes] == ["cp1", "n1", "cp2", "n4"]
    assert report.timings.nodes[1].cache == "miss" and report.timings.nodes[0].ms == 300


def test_report_without_outputs_or_run(db: Database) -> None:
    journal, _info = start_run(db, "r1")
    report = build_report(journal, "r1", FakeLinks(), Store())
    assert report.values is None
    assert report.failures == [] and report.checkpoints == [] and report.artifacts == []
    with pytest.raises(RunNotFound):
        build_report(journal, "missing", FakeLinks(), Store())
    with pytest.raises(RunNotFound):
        journal_page(journal, "missing", FakeLinks())


def test_timings(db: Database) -> None:
    created = 1_000.0
    journal, _info = start_run(db, "r1", created_at=created)
    offsets = [("run.queued", 0.0), ("run.preparing", 1.0), ("run.running", 3.0), ("node.started", 3.5),
               ("run.paused", 4.0), ("run.running", 6.0), ("run.paused", 7.0), ("run.running", 7.5),
               ("run.passed", 10.0)]
    for type, offset in offsets:
        event = journal.append("r1", NodeEventIn(type=type, node_id="n0" if type.startswith("node.") else None))
        set_ts(db, event.id, created + offset)
    journal.set_status("r1", "passed", finished_at=created + 10.0)
    for index in range(SLOWEST_NODES + 5):
        journal.upsert_node(record("r1", f"n{index}", index, duration_ms=(index * 7) % 30 + 1))
    journal.upsert_node(record("r1", "pending", 99, status="pending"))
    timings = build_report(journal, "r1", FakeLinks(), Store()).timings
    assert timings.total_ms == 10_000
    assert timings.queued_ms == 1_000
    assert timings.preparing_ms == 2_000
    assert timings.paused_ms == 2_500
    assert timings.running_ms == 7_000
    assert len(timings.nodes) == SLOWEST_NODES
    durations = [timing.ms for timing in timings.nodes]
    assert durations == sorted(durations, reverse=True)
    every = sorted(((index * 7) % 30 + 1 for index in range(SLOWEST_NODES + 5)), reverse=True)
    assert durations == every[:SLOWEST_NODES]

    journal2, _ = start_run(db, "r2", created_at=created)
    event = journal2.append("r2", NodeEventIn(type="run.queued"))
    set_ts(db, event.id, created)
    journal2.set_status("r2", "aborted", reason="cancelled before it started", finished_at=created + 4.0)
    queued_only = build_report(journal2, "r2", FakeLinks(), Store()).timings
    assert (queued_only.total_ms, queued_only.queued_ms, queued_only.preparing_ms, queued_only.running_ms) == (
        4_000, 4_000, 0, 0)


def test_run_level_failures(db: Database) -> None:
    journal, _ = start_run(db, "err")
    journal.upsert_node(record("err", "n1", 1, status="soft_failed",
                               error=NodeError(code="assertion", message="soft", soft=True)))
    journal.set_status("err", "error", reason="resource_error: no browser")
    failures = build_report(journal, "err", FakeLinks(), Store()).failures
    assert [(failure.node, failure.code, failure.soft) for failure in failures] == [
        ("n1", "assertion", True), (None, "run_error", False)]
    assert failures[-1].message == "resource_error: no browser"

    start_run(db, "abort")
    journal.set_status("abort", "aborted", reason="aborted by alice")
    (aborted,) = build_report(journal, "abort", FakeLinks(), Store()).failures
    assert aborted.node is None and aborted.code == "run_aborted" and aborted.message == "aborted by alice"

    start_run(db, "hard")
    journal.upsert_node(record("hard", "n1", 1, status="failed", error=NodeError(code="timeout", message="slow")))
    journal.set_status("hard", "aborted", reason="aborted")
    (only,) = build_report(journal, "hard", FakeLinks(), Store()).failures
    assert only.node == "n1" and only.code == "timeout"

    start_run(db, "passed")
    journal.set_status("passed", "passed")
    assert build_report(journal, "passed", FakeLinks(), Store()).failures == []


def test_journal_page(db: Database) -> None:
    journal, _ = start_run(db, "r1")
    journal.upsert_node(record("r1", "n1", 1))
    expansion = journal.record_expansion("r1", "each1", "", "each", {"items": [1]})
    resource = ResourceRecord(run_id="r1", resource_id="slot", kind="slot", state="held")
    journal.set_resource(resource)
    appended = [journal.append("r1", NodeEventIn(type="node.log", node_id="n1", detail={"i": index}))
                for index in range(5)]

    plain = journal_page(journal, "r1", FakeLinks())
    assert plain.run.id == "r1"
    assert [node.node_id for node in plain.nodes] == ["n1"]
    assert plain.expansions == [expansion] and plain.resources == [resource]
    assert plain.events == [] and plain.next_after is None

    first = journal_page(journal, "r1", FakeLinks(), events=True, limit=2)
    assert first.events == appended[:2] and first.next_after == appended[1].id
    second = journal_page(journal, "r1", FakeLinks(), events=True, after=first.next_after, limit=2)
    assert second.events == appended[2:4] and second.next_after == appended[3].id
    last = journal_page(journal, "r1", FakeLinks(), events=True, after=second.next_after, limit=2)
    assert last.events == appended[4:] and last.next_after is None
    exact = journal_page(journal, "r1", FakeLinks(), events=True, limit=5)
    assert exact.events == appended and exact.next_after is None
    pause = PauseInfo(reason="pause", at=1.0)
    assert journal_page(journal, "r1", FakeLinks(), pause=pause).run.pause == pause


def artifact_dump(artifact_id: str, sha: str) -> dict[str, Any]:
    return Artifact(id=artifact_id, kind="screenshot", name="x.png", url=f"http://hub/a/{artifact_id}",
                    raw_url=f"http://hub/raw/{artifact_id}", sha256=sha).model_dump(mode="json")


def test_compare_runs() -> None:
    def pair(**fields: Any) -> tuple[NodeRecord, NodeRecord]:
        left = record("a", "n1", 1, **fields)
        return left, left.model_copy(update={"run_id": "b"})

    a1, b1 = pair(output={"x": 1}, has_output=True)
    same = compare_runs("a", [a1], "b", [b1])
    assert same.same is True and same.diverging == [] and same.a == "a" and same.b == "b"

    a_status = record("a", "n1", 1)
    b_status = record("b", "n1", 1, status="failed", error=NodeError(code="timeout", message="slow"))
    fields = {(d.field, d.a, d.b) for d in compare_runs("a", [a_status], "b", [b_status]).diverging}
    assert fields == {("status", "passed", "failed"), ("error", None, "timeout")}

    a_out = record("a", "n2", 2, op="api.get", output={"id": 1}, has_output=True)
    b_out = record("b", "n2", 2, op="api.get", output={"id": 2}, has_output=True)
    (output,) = compare_runs("a", [a_out], "b", [b_out]).diverging
    assert (output.node, output.op, output.field, output.a, output.b) == ("n2", "api.get", "output", {"id": 1},
                                                                         {"id": 2})

    a_null = record("a", "n2", 2, output=None, has_output=True)
    b_absent = record("b", "n2", 2)
    (absent,) = compare_runs("a", [a_null], "b", [b_absent]).diverging
    assert absent.field == "output" and absent.a is None and absent.b is None

    a_err = record("a", "n3", 3, status="failed", error=NodeError(code="timeout", message="x"))
    b_err = record("b", "n3", 3, status="failed", error=NodeError(code="assertion", message="x"))
    (error,) = compare_runs("a", [a_err], "b", [b_err]).diverging
    assert (error.field, error.a, error.b) == ("error", "timeout", "assertion")

    a_art = record("a", "n4", 4, artifacts=["x1"])
    b_art = record("b", "n4", 4, artifacts=["y1", "y2"])
    (artifacts,) = compare_runs("a", [a_art], "b", [b_art]).diverging
    assert (artifacts.field, artifacts.a, artifacts.b) == ("artifacts", 1, 2)

    only_a = record("a", "n5", 5, instance="each#2")
    only_b = record("b", "n6", 6, status="skipped")
    missing = compare_runs("a", [only_a], "b", [only_b]).diverging
    assert [(d.node, d.instance, d.field, d.a, d.b) for d in missing] == [
        ("n5", "each#2", "status", "passed", None), ("n6", "", "status", None, "skipped")]

    a_shot = record("a", "n7", 7, op="capture.shot", output=artifact_dump("art-a", "sha256:same"), has_output=True,
                    artifacts=["art-a"])
    b_shot = record("b", "n7", 7, op="capture.shot", output=artifact_dump("art-b", "sha256:same"), has_output=True,
                    artifacts=["art-b"])
    assert compare_runs("a", [a_shot], "b", [b_shot]).same is True
    b_other = b_shot.model_copy(update={"output": artifact_dump("art-b", "sha256:other")})
    (changed,) = compare_runs("a", [a_shot], "b", [b_other]).diverging
    assert changed.field == "output" and changed.a == a_shot.output and changed.b == b_other.output

    def checkpoint(run: str, shots: list[str]) -> NodeRecord:
        outcome = CheckpointOutcome(label="list", ok=True, shots=shots)
        return record(run, "cp", 8, op="check.checkpoint", output=outcome.model_dump(mode="json", by_alias=True),
                      has_output=True, artifacts=list(shots))

    assert compare_runs("a", [checkpoint("a", ["s1", "s2"])], "b", [checkpoint("b", ["t1", "t2"])]).same is True
    fewer = compare_runs("a", [checkpoint("a", ["s1", "s2"])], "b", [checkpoint("b", ["t1"])]).diverging
    assert {d.field for d in fewer} == {"output", "artifacts"}
    output_divergence = next(d for d in fewer if d.field == "output")
    assert output_divergence.a == checkpoint("a", ["s1", "s2"]).output
    assert output_divergence.b == checkpoint("b", ["t1"]).output

    a_media = record("a", "m1", 9, op="media.source", has_output=True, artifacts=["ma"],
                     output={"uri": "artifact:ma", "kind": "video", "artifact": "ma"})
    b_media = record("b", "m1", 9, op="media.source", has_output=True, artifacts=["mb"],
                     output={"uri": "artifact:mb", "kind": "video", "artifact": "mb"})
    assert compare_runs("a", [a_media], "b", [b_media]).same is True
    foreign = b_media.model_copy(update={"output": {"uri": "artifact:zz", "kind": "video", "artifact": "zz"}})
    assert compare_runs("a", [a_media], "b", [foreign]).same is False

    order = compare_runs("a", [a_err, a_status], "b", [b_status, b_err]).diverging
    assert [d.node for d in order] == ["n1", "n1", "n3"]
