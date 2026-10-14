from __future__ import annotations

import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AuthEnv
from fastapi.testclient import TestClient

import eks_harness.engine.runs as engine_runs
from eks_harness.daemon.context import AppContext
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import ExecContext, ExecResult, ExecutorSpec
from eks_harness.engine.model import (
    TERMINAL_STATUSES,
    EnvironmentOut,
    RunInfo,
    RunOptions,
)
from eks_harness.engine.runs import RECOVERY_REASON, RunManager, engine
from eks_harness.ir.registry import Registry, default_registry

PROJECT = "acme/runs"
FAKES = {"families": ["probe"]}

PROBES = """
    from collections.abc import Mapping
    from typing import Any

    import eks
    from eks_harness.ir.refs import RJson
    from eks_harness.plugins.sdk import BackendInfo, BackendSpec


    @eks.op("probe", name="echo", out="json", pure=True, description="Echo a value")
    class Echo(eks.Args):
        value: RJson = None


    @eks.op("probe", name="step", out="json", description="A step that may wait or fail")
    class Step(eks.Args):
        value: RJson = None
        seconds: float = 0.0
        fail: bool = False


    @eks.op("lonely", name="op", out="json", description="An op no executor serves")
    class Lonely(eks.Args):
        value: int = 1


    @eks.backend("testapi", description="The backend the run tests use")
    class TestApi:
        def describe(self) -> dict[str, Any]:
            return {"name": "testapi"}

        def fingerprint(self, context: Mapping[str, Any]) -> str:
            return "testapi"

        def prepare(self, context: Mapping[str, Any]) -> None:
            return None

        def spec(self, context: Mapping[str, Any]) -> BackendSpec:
            return BackendSpec(info=BackendInfo(url="http://127.0.0.1:9"))

        def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
            return None
"""

PURE_FLOW = """
    import eks


    @eks.flow(app="web-app", backend="testapi")
    def pure(s: eks.Session, word: str = "hi"):
        a = s.op("probe.echo", value=word)
        b = s.op("probe.step", value=a)
        return {"a": a, "b": b}
"""

SLOW_FLOW = """
    import eks


    @eks.flow(app="web-app", backend="testapi")
    def slow(s: eks.Session, seconds: float = 30.0):
        return {"value": s.op("probe.step", value="slow", seconds=seconds)}
"""

LONELY_FLOW = """
    import eks


    @eks.flow(app="web-app", backend="testapi")
    def lonely(s: eks.Session):
        s.op("lonely.op", value=2)
"""

BROKEN_FLOW = """
    import eks


    @eks.flow(app="web-app", backend="testapi")
    def broken(s: eks.Session):
        s.op("probe.echo", value=1)
        s.op("probe.nowhere", value=2)
"""


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def make_tree(root: Path, project: str = PROJECT) -> Path:
    write(root / ".harness" / "project.toml", f'[project]\nid = "{project}"\n')
    write(root / "web-app" / ".harness" / "app.toml",
          '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n')
    write(root / ".harness" / "seeds" / "probes.py", PROBES)
    write(root / ".harness" / "flows" / "pure.py", PURE_FLOW)
    write(root / ".harness" / "flows" / "slow.py", SLOW_FLOW)
    write(root / ".harness" / "flows" / "lonely.py", LONELY_FLOW)
    return root.resolve()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "tree")


class Probe:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, Any]] = []
        self.started = threading.Event()

    def run(self, call: ExecContext) -> ExecResult:
        with self.lock:
            self.calls.append((call.op, call.data.get("value")))
        self.started.set()
        seconds = call.data.get("seconds")
        if isinstance(seconds, int | float) and seconds > 0:
            call.cancel.wait(float(seconds))
            call.check_cancelled()
        if call.data.get("fail") is True:
            raise NodeFailure("probe_failure", "the probe was asked to fail")
        return ExecResult(value=call.data.get("value"))


def install(ctx: AppContext) -> tuple[RunManager, Probe]:
    manager = engine(ctx)
    probe = Probe()
    manager.executors.register(ExecutorSpec("test.probe", "1", ops=("probe",), run=probe.run))
    return manager, probe


@pytest.fixture
def probe(client: TestClient, ctx: AppContext) -> Probe:
    return install(ctx)[1]


def body(tree: Path, spec: str = ".harness/flows/pure.py", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"spec": spec, "cwd": str(tree), "fakes": FAKES}
    payload.update(extra)
    return payload


def post_run(client: TestClient, payload: dict[str, Any], status: int = 201,
             headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = client.post("/api/runs", json=payload, headers=headers or {})
    assert response.status_code == status, response.text
    return response.json()


def wait_status(client: TestClient, run_id: str, wanted: set[str], timeout: float = 30.0,
                headers: dict[str, str] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        found = client.get(f"/api/runs/{run_id}", params={"wait": 1}, headers=headers or {}).json()
        if found["status"] in wanted:
            return found
        assert time.monotonic() < deadline, f"run {run_id} stayed {found['status']}"
        time.sleep(0.02)


def plan_nodes(client: TestClient, tree: Path, spec: str = ".harness/flows/pure.py") -> list[dict[str, Any]]:
    response = client.post("/api/plans", json={"spec": spec, "cwd": str(tree)})
    assert response.status_code == 200, response.text
    return response.json()["plan"]["nodes"]


def control(client: TestClient, run_id: str, **request: Any) -> dict[str, Any]:
    response = client.post(f"/api/runs/{run_id}/control", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def events_of(client: TestClient, run_id: str, type: str | None = None) -> list[dict[str, Any]]:
    page = client.get(f"/api/runs/{run_id}/journal", params={"events": "true", "limit": 5000}).json()
    return [event for event in page["events"] if type is None or event["type"] == type]


def test_runs_execute_and_every_read_endpoint_answers(client: TestClient, tree: Path, probe: Probe) -> None:
    created = post_run(client, body(tree, wait=30))
    run = created["run"]
    assert run["status"] == "passed", run
    assert run["project"] == PROJECT and run["simulated"] is True
    assert run["plan"]["name"] == "pure" and run["spec"] == ".harness/flows/pure.py"
    assert run["links"]["report"].endswith(f"/api/runs/{run['id']}/report")
    assert [issue["code"] for issue in created["issues"]] == ["clock.now"]
    assert [value for _op, value in probe.calls] == ["hi", "hi"]

    summary = client.get(f"/api/runs/{run['id']}").json()
    assert summary["status"] == "passed" and summary["counts"]["passed"] == 2
    assert summary["cache"]["stored"] == 1 and summary["finishedAt"] is not None

    report = client.get(f"/api/runs/{run['id']}/report").json()
    assert report["values"] == {"a": "hi", "b": "hi"}
    assert report["failures"] == [] and report["journal"]["nodes"] == 2
    assert [issue["code"] for issue in report["issues"]] == ["clock.now"]
    assert report["timings"]["totalMs"] >= report["timings"]["runningMs"] >= 0
    assert {item["kind"] for item in report["resources"]} >= {"backend", "lease.web"}
    assert all(item["state"] == "released" for item in report["resources"])

    plan = client.get(f"/api/runs/{run['id']}/plan").json()
    assert plan["hash"] == run["plan"]["hash"] and plan["name"] == "pure"

    journal = client.get(f"/api/runs/{run['id']}/journal", params={"events": "true"}).json()
    assert [node["status"] for node in journal["nodes"]] == ["passed", "passed"]
    types = [event["type"] for event in journal["events"]]
    assert types[0] == "run.queued" and types[-1] == "run.passed"
    assert types.index("run.preparing") < types.index("run.running") < types.index("node.started")
    paged = client.get(f"/api/runs/{run['id']}/journal", params={"events": "true", "limit": 2}).json()
    assert len(paged["events"]) == 2 and paged["nextAfter"] == paged["events"][-1]["id"]
    rest = client.get(f"/api/runs/{run['id']}/journal",
                      params={"events": "true", "after": paged["nextAfter"], "limit": 5000}).json()
    assert [event["id"] for event in paged["events"] + rest["events"]] == [event["id"] for event in
                                                                             journal["events"]]

    second = post_run(client, body(tree, wait=30, params={"word": "again"}))["run"]
    assert second["status"] == "passed"
    failing = post_run(client, body(tree, ".harness/flows/slow.py", wait=30, params={"seconds": "0"},
                                    options={"cache": "off"}))["run"]
    assert failing["status"] == "passed"

    listed = client.get("/api/runs", params={"project": PROJECT}).json()
    assert [item["id"] for item in listed["items"]] == [failing["id"], second["id"], run["id"]]
    assert listed["nextCursor"] is None
    named = client.get("/api/runs", params={"name": "pure", "status": "passed"}).json()
    assert [item["id"] for item in named["items"]] == [second["id"], run["id"]]
    by_hash = client.get("/api/runs", params={"plan": run["plan"]["hash"]}).json()
    assert [item["id"] for item in by_hash["items"]] == [run["id"]]
    first_page = client.get("/api/runs", params={"limit": 2}).json()
    assert len(first_page["items"]) == 2 and first_page["nextCursor"]
    second_page = client.get("/api/runs", params={"limit": 2, "cursor": first_page["nextCursor"]}).json()
    assert [item["id"] for item in second_page["items"]] == [run["id"]]
    assert second_page["nextCursor"] is None
    assert client.get("/api/runs", params={"status": "failed"}).json()["items"] == []
    bad = client.get("/api/runs", params={"cursor": "not-a-cursor!"})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_cursor"
    missing = client.get("/api/runs/01JNOSUCHRUN0000000000000")
    assert missing.status_code == 404 and missing.json()["error"] == "run_not_found"
    for path in ("report", "plan", "journal", "events"):
        assert client.get(f"/api/runs/01JNOSUCHRUN0000000000000/{path}").status_code == 404


def test_refusals_carry_issues(client: TestClient, tree: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = client.post("/api/runs", json={})
    assert empty.status_code == 422
    assert empty.json()["error"] == "run_refused"
    assert [issue["code"] for issue in empty.json()["issues"]] == ["plan.source"]

    malformed = client.post("/api/runs", json={"plan": {"schema": "eks/plan@1", "kind": "flow", "bogus": 1}})
    assert malformed.status_code == 422
    (issue,) = malformed.json()["issues"]
    assert issue["code"] == "plan.json" and "does not parse" in issue["message"]

    monkeypatch.setattr(engine_runs, "worker_driver_problem", lambda platform: "this test hub has no workers")
    lonely = client.post("/api/runs", json={"spec": ".harness/flows/lonely.py", "cwd": str(tree)})
    assert lonely.status_code == 422
    codes = [item["code"] for item in lonely.json()["issues"]]
    assert "executor.missing" in codes and "driver.missing" in codes
    missing = next(item for item in lonely.json()["issues"] if item["code"] == "executor.missing")
    assert "lonely.op" in missing["message"] and "run with fakes" in missing["message"]

    write(tree / ".harness" / "flows" / "broken.py", BROKEN_FLOW)
    broken = client.post("/api/runs", json=body(tree, ".harness/flows/broken.py"))
    assert broken.status_code == 400, broken.text
    payload = broken.json()
    assert payload["error"] == "plan_build" and "probe.nowhere" in payload["message"]
    assert payload["where"].startswith(".harness/flows/broken.py:7")

    bad_break = client.post("/api/runs", json=body(tree, options={"breakpoints": ["op:"]}))
    assert bad_break.status_code == 422
    assert [item["code"] for item in bad_break.json()["issues"]] == ["debug.breakpoint"]
    assert client.get("/api/runs").json()["items"] == []
    assert probe.calls == []


def test_debugging_over_http(client: TestClient, tree: Path, probe: Probe) -> None:
    first, second = (node["id"] for node in plan_nodes(client, tree))
    paused = post_run(client, body(tree, wait=30, options={"breakpoints": [first], "cache": "off"}))["run"]
    run_id = paused["id"]
    assert paused["status"] == "paused" and paused["pause"]["node"] == first
    assert paused["pause"]["reason"] == "breakpoint"
    shown = client.get(f"/api/runs/{run_id}").json()
    assert shown["status"] == "paused" and shown["pause"]["node"] == first
    status = control(client, run_id, action="status")
    assert status["ok"] and status["status"] == "paused" and status["debugger"] == "paused"
    assert [item["text"] for item in status["breakpoints"]] == [first]
    resumed = control(client, run_id, action="continue")
    assert resumed["ok"], resumed
    assert wait_status(client, run_id, set(TERMINAL_STATUSES))["status"] == "passed"
    controls = events_of(client, run_id, "control")
    assert [(event["detail"]["action"], event["detail"]["actor"]) for event in controls] == [
        ("status", "local"), ("continue", "local")]
    finished = client.post(f"/api/runs/{run_id}/control", json={"action": "continue"})
    assert finished.status_code == 409 and finished.json()["error"] == "run_finished"

    stepping = post_run(client, body(tree, wait=30, options={"breakpoints": [first], "cache": "off"}))["run"]
    stepped = control(client, stepping["id"], action="step")
    assert stepped["ok"], stepped
    at_second = wait_status(client, stepping["id"], {"paused"})
    assert at_second["pause"]["node"] == second and at_second["pause"]["reason"] == "step"
    control(client, stepping["id"], action="continue")
    assert wait_status(client, stepping["id"], set(TERMINAL_STATUSES))["status"] == "passed"

    edited = post_run(client, body(tree, wait=30, options={"breakpoints": [first], "cache": "off"}))["run"]
    changed = control(client, edited["id"], action="edit", args={"value": "edited"})
    assert changed["ok"], changed
    assert changed["status"] == "paused"
    retried = control(client, edited["id"], action="retry")
    assert retried["ok"], retried
    assert wait_status(client, edited["id"], set(TERMINAL_STATUSES))["status"] == "passed"
    assert client.get(f"/api/runs/{edited['id']}/report").json()["values"] == {"a": "edited", "b": "edited"}
    assert events_of(client, edited["id"], "node.args_edited")

    skipping = post_run(client, body(tree, wait=30, options={"breakpoints": [first], "cache": "off"}))["run"]
    skipped = control(client, skipping["id"], action="skip", value="given")
    assert skipped["ok"], skipped
    assert wait_status(client, skipping["id"], set(TERMINAL_STATUSES))["status"] == "passed"
    nodes = client.get(f"/api/runs/{skipping['id']}/journal").json()["nodes"]
    assert [node["status"] for node in nodes] == ["skipped", "passed"]
    assert client.get(f"/api/runs/{skipping['id']}/report").json()["values"] == {"a": "given", "b": "given"}

    adhoc = post_run(client, body(tree, wait=30, options={"breakpoints": [first], "cache": "off"}))["run"]
    done = control(client, adhoc["id"], action="do", op="probe.step", args={"value": 41})
    assert done["ok"] and done["result"] == 41 and done["status"] == "paused"
    aborted = control(client, adhoc["id"], action="abort")
    assert aborted["ok"], aborted
    ended = wait_status(client, adhoc["id"], set(TERMINAL_STATUSES))
    assert ended["status"] == "aborted" and ended["reason"] == "aborted from the debugger"


def test_cancel_queued_and_running_runs(client: TestClient, ctx: AppContext, tree: Path, probe: Probe) -> None:
    ctx.config.set("engine.maxRuns", 1)
    first = post_run(client, body(tree, ".harness/flows/slow.py"))["run"]
    assert probe.started.wait(30)
    second = post_run(client, body(tree, ".harness/flows/slow.py"))["run"]
    assert second["status"] == "queued"
    assert control(client, second["id"], action="continue")["ok"] is False
    cancelled = client.post(f"/api/runs/{second['id']}/cancel", json={"reason": "not needed"}).json()
    assert cancelled["status"] == "aborted" and cancelled["reason"] == "cancelled before it started"
    assert wait_status(client, first["id"], {"running"})["status"] == "running"
    stopped = client.post(f"/api/runs/{first['id']}/cancel").json()
    assert stopped["status"] == "aborted" and stopped["reason"] == "cancelled by local"
    third = post_run(client, body(tree, ".harness/flows/slow.py", wait=30, params={"seconds": "0"}))["run"]
    assert third["status"] == "passed"
    again = client.post(f"/api/runs/{first['id']}/cancel").json()
    assert again["status"] == "aborted"


def test_replay_and_compare(client: TestClient, tree: Path, probe: Probe) -> None:
    run = post_run(client, body(tree, wait=30, params={"word": "replayed"}))["run"]
    response = client.post(f"/api/runs/{run['id']}/replay")
    assert response.status_code == 201, response.text
    replayed = response.json()["run"]
    assert replayed["replayOf"] == run["id"] and replayed["plan"]["hash"] == run["plan"]["hash"]
    assert replayed["spec"] is None
    done = wait_status(client, replayed["id"], set(TERMINAL_STATUSES))
    assert done["status"] == "passed" and done["cache"]["hits"] == 1
    compared = client.get(f"/api/runs/{run['id']}/compare/{replayed['id']}").json()
    assert compared == {"a": run["id"], "b": replayed["id"], "same": True, "diverging": []}
    other = post_run(client, body(tree, wait=30, params={"word": "different"}))["run"]
    diverging = client.get(f"/api/runs/{run['id']}/compare/{other['id']}").json()
    assert not diverging["same"] and {item["field"] for item in diverging["diverging"]} == {"output"}
    assert client.get(f"/api/runs/{run['id']}/compare/01JNOSUCHRUN0000000000000").status_code == 404
    assert client.post("/api/runs/01JNOSUCHRUN0000000000000/replay").status_code == 404


def frames(text: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        found.append(fields)
    return found


def test_event_stream_replays_and_resumes(client: TestClient, tree: Path, probe: Probe) -> None:
    run = post_run(client, body(tree, wait=30))["run"]
    response = client.get(f"/api/runs/{run['id']}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    parsed = frames(response.text)
    assert parsed[0] == {"retry": "3000"}
    assert parsed[-1] == {"event": "end", "data": "passed"}
    ids = [int(frame["id"]) for frame in parsed[1:-1]]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    kinds = [frame["event"] for frame in parsed[1:-1]]
    assert kinds[0] == "run" and "node" in kinds and "resource" in kinds
    middle = ids[len(ids) // 2]
    resumed = frames(client.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": str(middle)}).text)
    assert [int(frame["id"]) for frame in resumed[1:-1]] == [item for item in ids if item > middle]
    assert resumed[-1] == {"event": "end", "data": "passed"}
    bad = client.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": "x1"})
    assert bad.status_code == 400


def test_authorization(auth_env: AuthEnv, tmp_path: Path) -> None:
    manager, _probe = install(auth_env.app.state.ctx)
    tree = make_tree(tmp_path / "tree")
    other = make_tree(tmp_path / "other", project="acme/other")
    client = auth_env.client
    admin = auth_env.admin_headers
    run = post_run(client, body(tree, wait=30), headers=admin)["run"]
    assert run["status"] == "passed"

    stranger, stranger_key = auth_env.make_user("stranger")
    del stranger
    outsider = auth_env.headers_for(stranger_key)
    assert client.get(f"/api/runs/{run['id']}", headers=outsider).status_code == 404
    assert client.get(f"/api/runs/{run['id']}/report", headers=outsider).status_code == 404
    assert client.get("/api/runs", headers=outsider).json()["items"] == []
    refused = client.post("/api/runs", json=body(tree), headers=outsider)
    assert refused.status_code in (403, 404)
    assert client.post("/api/plans", json={"spec": ".harness/flows/pure.py", "cwd": str(tree)},
                       headers=outsider).status_code in (403, 404)

    viewer, viewer_key = auth_env.make_user("viewer")
    auth_env.grant(viewer, PROJECT, "viewer")
    watching = auth_env.headers_for(viewer_key)
    assert client.get(f"/api/runs/{run['id']}", headers=watching).status_code == 200
    assert client.get(f"/api/runs/{run['id']}/journal", headers=watching).status_code == 200
    assert [item["id"] for item in client.get("/api/runs", headers=watching).json()["items"]] == [run["id"]]
    blocked = client.post(f"/api/runs/{run['id']}/control", json={"action": "status"}, headers=watching)
    assert blocked.status_code == 403 and blocked.json()["error"] == "insufficient_grant"
    assert client.post(f"/api/runs/{run['id']}/cancel", headers=watching).status_code == 403
    assert client.post(f"/api/runs/{run['id']}/replay", headers=watching).status_code == 403
    assert client.post("/api/runs", json=body(tree), headers=watching).status_code == 403
    planned = client.post("/api/plans", json={"spec": ".harness/flows/pure.py", "cwd": str(tree)},
                          headers=watching)
    assert planned.status_code == 200, planned.text

    editor, editor_key = auth_env.make_user("editor")
    auth_env.grant(editor, PROJECT, "editor")
    editing = auth_env.headers_for(editor_key)
    own = post_run(client, body(tree, wait=30), headers=editing)["run"]
    assert own["status"] == "passed" and own["actor"] == "editor"
    replayed = client.post(f"/api/runs/{run['id']}/replay", headers=editing)
    assert replayed.status_code == 201, replayed.text
    foreign = client.post("/api/runs", json=body(other), headers=editing)
    assert foreign.status_code in (403, 404)
    inline = client.post("/api/plans", json={"source": PURE_FLOW, "tree": str(tree)}, headers=editing)
    assert inline.status_code == 403 and inline.json()["error"] == "admin_required"
    inline_run = client.post("/api/runs", json={"source": PURE_FLOW, "tree": str(tree), "fakes": FAKES},
                             headers=editing)
    assert inline_run.status_code == 403
    wrong_target = client.post("/api/runs", json=body(tree, project="acme/other"), headers=editing)
    assert wrong_target.status_code in (403, 404)
    admin_inline = client.post("/api/plans", json={"source": textwrap.dedent(PURE_FLOW), "tree": str(tree)},
                               headers=admin)
    assert admin_inline.status_code == 200, admin_inline.text
    assert manager.journal.run(run["id"]) is not None


def test_recover_and_maintain(client: TestClient, ctx: AppContext, tree: Path, probe: Probe) -> None:
    manager = engine(ctx)
    run = post_run(client, body(tree, wait=30))["run"]
    row = manager.journal.run(run["id"])
    assert row is not None
    stale = RunInfo(id="01JSTALERUN000000000000000", project=PROJECT, session=row.info.session,
                    session_id=row.info.session_id, plan_hash=row.info.plan_hash, plan_kind="flow",
                    plan_name="pure", options=RunOptions(), created_at=time.time() - 60)
    manager.journal.create_run(stale, row.plan, request={}, environment=EnvironmentOut(), simulated=False)
    manager.journal.set_status(stale.id, "running")
    assert manager.recover() == [stale.id]
    recovered = client.get(f"/api/runs/{stale.id}").json()
    assert recovered["status"] == "error" and recovered["reason"] == RECOVERY_REASON
    assert manager.recover() == []

    old = time.time() - 40 * 86400
    ancient = stale.model_copy(update={"id": "01JANCIENTRUN0000000000000", "created_at": old})
    manager.journal.create_run(ancient, row.plan, request={}, environment=EnvironmentOut(), simulated=False)
    manager.journal.set_status(ancient.id, "passed", finished_at=old)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE cache_entries SET last_used_at = ?", (old,))
    entries = ctx.db.conn().execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
    assert entries >= 1
    result = manager.maintain()
    assert result["runs"] == 1 and result["entries"] == entries
    assert manager.journal.run(ancient.id) is None
    assert manager.journal.run(run["id"]) is not None
    assert ctx.db.conn().execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 0


def test_shutdown_aborts_active_runs_and_releases_their_leases(client: TestClient, ctx: AppContext, tree: Path,
                                                               probe: Probe) -> None:
    manager = engine(ctx)
    ctx.config.set("engine.maxRuns", 1)
    running = post_run(client, body(tree, ".harness/flows/slow.py"))["run"]
    assert probe.started.wait(30)
    queued = post_run(client, body(tree, ".harness/flows/slow.py"))["run"]
    assert queued["status"] == "queued"
    owned = ctx.pools.backends.leases(owner=f"run:{running['id']}")
    assert owned
    manager.shutdown()
    for run_id in (running["id"], queued["id"]):
        ended = client.get(f"/api/runs/{run_id}").json()
        assert ended["status"] == "error" and ended["reason"] == "the hub shut down while the run was active"
    assert ctx.pools.backends.leases(owner=f"run:{running['id']}") == []
    resources = manager.journal.resources(running["id"])
    assert resources and all(item.state == "released" for item in resources)
    refused = client.post("/api/runs", json=body(tree))
    assert refused.status_code == 503 and refused.json()["error"] == "hub_stopping"
