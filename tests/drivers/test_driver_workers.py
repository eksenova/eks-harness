from __future__ import annotations

import json
import shutil
import time
import urllib.request
from pathlib import Path

import pytest

from eks_harness.drivers import WorkerClient, WorkerError, WorkerManager, workers_root
from eks_harness.drivers.sessions import WorkerSession
from eks_harness.drivers.workers import code_hash, config_hash, pid_alive

from fake_worker import FakeWorker

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_workers_root_points_at_sources() -> None:
    root = workers_root()
    assert (root / "web-driver/src/server.mjs").is_file()
    assert (root / "mobile-driver/src/server.mjs").is_file()
    assert code_hash("web", root) != code_hash("mobile", root)
    assert config_hash({"a": 1, "port": 5}) == config_hash({"a": 1, "port": 9, "stateDir": "x"})


@needs_node
def test_manager_spawns_reuses_restarts_and_stops_a_real_worker(paths, tmp_path: Path) -> None:
    manager = WorkerManager(paths, env={"EKS_HARNESS_URL": "http://127.0.0.1:9"})
    config = {"sids": {"ios": "sidA"}, "fakeSlots": ["nfc"], "options": {"heartbeatMs": 60000}}
    first = manager.ensure("mobile", "sidA", config)
    try:
        assert pid_alive(first.pid) and not first.reused
        status, health = first.client().get("/health")
        assert status == 200 and health["sids"] == {"ios": "sidA"}
        again = manager.ensure("mobile", "sidA", config)
        assert again.reused and again.pid == first.pid
        assert [h.key for h in manager.list()] == ["sidA"]
        changed = manager.ensure("mobile", "sidA", {**config, "fakeSlots": ["camera"]})
        assert changed.pid != first.pid and not changed.reused
        assert not pid_alive(first.pid) or _wait_dead(first.pid)
        status, armed = changed.client().post("/arm", {"slot": "camera", "platform": "ios"})
        assert status == 200 and armed["slot"] == "camera"
        config_file = changed.state_dir / "config.json"
        assert oct(config_file.stat().st_mode & 0o777) == "0o600"
        assert json.loads(config_file.read_text())["sids"] == {"ios": "sidA"}
    finally:
        assert manager.stop_all() == 1
    assert manager.list() == []


def _wait_dead(pid: int) -> bool:
    for _ in range(50):
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@needs_node
def test_manager_reports_a_worker_that_cannot_start(paths, tmp_path: Path) -> None:
    root = tmp_path / "workers"
    (root / "mobile-driver/src").mkdir(parents=True)
    (root / "mobile-driver/src/server.mjs").write_text("console.log('boom'); process.exit(4)\n")
    manager = WorkerManager(paths, root=root)
    with pytest.raises(WorkerError, match="exited with 4"):
        manager.ensure("mobile", "x", {})


def test_session_maps_actions_observations_and_captures() -> None:
    answers = {"press": "pressed", "patch": {"key": "p1"}, "route": {"name": "Home"}, "shot": {"id": "a1"},
               "video.stop": {"id": "v1"}, "evaluate": 42, "arm": {"armed": "nfc"}, "annotate.caption": "caption"}
    with FakeWorker(answers) as fake:
        session = WorkerSession(WorkerClient(fake.url), platform="ios", sid="s1")
        assert session.act("tap", target="#go")["value"] == "pressed"
        assert session.act("patch", target="#title", text="Hi", style={"color": "red"})["value"] == {"key": "p1"}
        assert session.act("arm", fake="nfc", payload={"mode": "error"})["value"] == {"armed": "nfc"}
        assert session.act("evaluate", script="return 42")["value"] == 42
        assert session.act("mark", name="beat 4")["value"] == "caption"
        assert session.observe("route")["value"] == {"name": "Home"}
        assert session.capture("screenshot", name="home")["value"] == {"id": "a1"}
        assert session.capture("video.stop", trim=False)["value"] == {"id": "v1"}
        assert fake.calls[0][:2] == ("press", ["#go"])
        assert fake.calls[1][:2] == ("patch", ["#title", {"text": "Hi", "style": {"color": "red"}}])
        assert fake.calls[0][2] == {"script": 'return await press("#go")', "sid": "s1", "platform": "ios"}
        with pytest.raises(WorkerError):
            session.capture("har.start")
        fake.events.append({"name": "result-shown", "source": "ios"})
        assert next(iter(session.events(timeout=5)))["name"] == "result-shown"


def test_web_session_uses_web_helpers() -> None:
    with FakeWorker({"nav": "/next", "fill": True, "tree": "- main"}) as fake:
        session = WorkerSession(WorkerClient(fake.url), platform="web", sid="w1")
        session.act("navigate", route="/next")
        session.act("fill", target="#name", text="Ada")
        assert session.observe("tree")["value"] == "- main"
        assert [c[0] for c in fake.calls] == ["nav", "fill", "tree"]
        assert "platform" not in fake.calls[0][2]


def test_worker_client_reports_unreachable_workers() -> None:
    status, body = WorkerClient("http://127.0.0.1:9").post("/exec", {}, 2)
    assert status == 0 and "not reachable" in body["error"]
    with pytest.raises(WorkerError, match="not reachable"):
        WorkerClient("http://127.0.0.1:9").call("click", "x", timeout=2)
    assert urllib.request is not None
