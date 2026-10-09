from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from eks_harness.daemon.app import create_app
from eks_harness.nodes.agent import NodeAgent, NodeSettings
from eks_harness.nodes.blobs import BlobStore, sha256_file
from eks_harness.nodes.capabilities import default_slots, names, satisfies


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_hub(config):
    port = free_port()
    config.set("server.port", port)
    app = create_app(config, config.paths, fake_pools=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield app.state.ctx, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


class AgentThread:
    def __init__(self, agent: NodeAgent) -> None:
        self.agent = agent
        self.thread = threading.Thread(target=lambda: asyncio.run(agent.run()), daemon=True)

    def __enter__(self) -> NodeAgent:
        self.thread.start()
        assert self.agent.connected.wait(20)
        return self.agent

    def __exit__(self, *_) -> None:
        self.agent.stop()
        self.thread.join(10)


def make_agent(tmp_path: Path, base: str, token: str, slots=None) -> NodeAgent:
    settings = NodeSettings(hub=base, token=token, name="n1", cache_dir=str(tmp_path / "node-cache"),
                            slots=slots or [{"id": "gpu0", "workers": 1, "tags": ["gpu", "cuda"],
                                             "env": {"CUDA_VISIBLE_DEVICES": "0"}, "backend": "CUDA"},
                                            {"id": "cpu", "workers": 2, "tags": ["cpu"], "env": {}}])
    from eks_harness.paths import resolve_paths
    agent = NodeAgent(settings, resolve_paths(), handlers={})
    agent.refresh_capabilities = lambda: (setattr(agent, "capabilities", {"os": "linux", "cpus": 8,
                                                                          "jobKinds": sorted(agent.handlers)}),
                                          setattr(agent, "slots", settings.slots))
    return agent


def wait_job(client: httpx.Client, job_id: str) -> dict:
    return client.get(f"/api/jobs/{job_id}", params={"wait": 30}).json()


def test_node_roundtrip_jobs_blobs_and_slots(live_hub, tmp_path: Path) -> None:
    ctx, base = live_hub
    with httpx.Client(base_url=base, timeout=60) as client:
        created = client.post("/api/nodes", json={"id": "gpu-box", "label": "GPU box"}).json()
        token = created["token"]
        assert token.startswith("ehn_") and created["node"]["id"] == "gpu-box"
        assert client.post("/api/nodes", json={"id": "gpu-box"}).status_code == 409
        uploaded = client.post("/api/blobs", content=b"hello from the hub").json()
        with AgentThread(make_agent(tmp_path, base, token)):
            status = client.get("/api/nodes").json()["items"][0]
            assert status["online"] and [s["id"] for s in status["slots"]] == ["gpu0", "cpu"]
            job = client.post("/api/jobs", json={
                "kind": "echo", "payload": {"value": 42, "write": "rendered", "steps": 3, "envKeys": ["CUDA_VISIBLE_DEVICES"]},
                "requirements": {"gpu": True}, "inputs": [{"name": "greeting", "hash": uploaded["hash"],
                                                           "path": "in/greeting.txt"}]}).json()
            done = wait_job(client, job["id"])
            assert done["state"] == "done" and done["node"] == "gpu-box" and done["slot"] == "gpu0"
            data = done["result"]["data"]
            assert data["echo"] == 42 and data["inputs"]["greeting"] == "hello from the hub"
            assert data["env"] == {"CUDA_VISIBLE_DEVICES": "0"}
            output = done["result"]["outputs"][0]
            assert client.get(f"/api/blobs/{output['hash']}").content == b"rendered"
            failing = client.post("/api/jobs", json={"kind": "echo", "payload": {"fail": "boom"}}).json()
            assert wait_job(client, failing["id"])["state"] == "failed"
            unknown = client.post("/api/jobs", json={"kind": "nope"}).json()
            assert "cannot run" in wait_job(client, unknown["id"])["error"]
            slow = client.post("/api/jobs", json={"kind": "echo", "payload": {"sleep": 30, "steps": 300}}).json()
            time.sleep(0.5)
            assert client.post(f"/api/jobs/{slow['id']}/cancel").json()["state"] == "cancelled"
            impossible = client.post("/api/jobs", json={"kind": "echo", "requirements": {"capabilities": ["blender"]}}).json()
            time.sleep(0.3)
            assert client.get(f"/api/jobs/{impossible['id']}").json()["state"] == "queued"
            assert client.post("/api/jobs", json={"kind": "echo", "inputs": [{"name": "x", "hash": "0" * 64}]}).status_code == 400
        assert client.get("/api/nodes").json()["items"][0]["online"] is False
        assert client.get("/api/blobs/" + uploaded["hash"], headers={"Authorization": "Bearer ehn_bad_token_xxxxxxxxxxxxxxxxxxxxx"}).status_code == 401


def test_jobs_requeue_when_node_drops(live_hub, tmp_path: Path) -> None:
    ctx, base = live_hub
    with httpx.Client(base_url=base, timeout=60) as client:
        token = client.post("/api/nodes", json={"id": "flaky"}).json()["token"]
        agent = make_agent(tmp_path, base, token, slots=[{"id": "cpu", "workers": 1, "tags": ["cpu"]}])
        with AgentThread(agent):
            job = client.post("/api/jobs", json={"kind": "echo", "payload": {"sleep": 30, "steps": 300}}).json()
            time.sleep(0.5)
            assert client.get(f"/api/jobs/{job['id']}").json()["state"] in ("assigned", "running")
        time.sleep(0.5)
        after = client.get(f"/api/jobs/{job['id']}").json()
        assert after["state"] == "queued" and after["message"] == "node disconnected"
        rotated = client.post("/api/nodes/flaky/token").json()["token"]
        assert rotated != token
        assert client.delete("/api/nodes/flaky").json() == {"removed": "flaky"}


def test_blob_store_and_capability_matching(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    source = tmp_path / "a.bin"
    source.write_bytes(b"x" * 3000)
    digest, size = store.put_file(source)
    assert digest == sha256_file(source) and size == 3000 and store.has(digest)
    target = store.materialize(digest, tmp_path / "work" / "a.bin")
    assert target.read_bytes() == b"x" * 3000
    with pytest.raises(ValueError):
        store.put_stream(iter([b"abc"]), expected="0" * 64)
    caps = {"os": "linux", "cpus": 16, "gpus": [{"index": 0, "name": "RTX", "vendor": "nvidia",
                                                 "backends": ["CUDA", "OPTIX"]}], "blender": {"path": "/b"}}
    slots = default_slots(caps)
    assert [s["id"] for s in slots] == ["gpu0", "cpu"] and slots[0]["env"] == {"CUDA_VISIBLE_DEVICES": "0"}
    assert {"blender", "gpu", "optix", "linux"} <= names(caps)
    assert satisfies(caps, slots[0], {"capabilities": ["blender"], "gpu": True, "backend": "optix"})
    assert not satisfies(caps, slots[1], {"gpu": True})
    assert not satisfies(caps, slots[0], {"os": "darwin"})


def test_node_cli_configure_and_listing(live_hub, tmp_path: Path, capsys, monkeypatch) -> None:
    import io
    import json as jsonlib

    from eks_harness.cli import main
    from eks_harness.nodes.agent import load_settings
    from eks_harness.paths import resolve_paths

    ctx, base = live_hub
    assert main(["--url", base, "node", "create", "cli-node", "--json"]) == 0
    created = jsonlib.loads(capsys.readouterr().out)
    monkeypatch.setattr("sys.stdin", io.StringIO(created["token"] + "\n"))
    assert main(["node", "configure", "--hub", base, "--name", "cli-node", "--token-stdin",
                 "--slots", '[{"id": "cpu", "workers": 3}]', "--env", "A=1"]) == 0
    settings = load_settings(resolve_paths())
    assert settings.token == created["token"] and settings.slots == [{"id": "cpu", "workers": 3}]
    assert settings.env == {"A": "1"} and settings.ws_url == base.replace("http", "ws") + "/api/nodes/connect"
    assert main(["--url", base, "node", "list", "--json"]) == 0
    listed = jsonlib.loads(capsys.readouterr().out)
    assert [n["id"] for n in listed["items"]] == ["cli-node"] and listed["items"][0]["state"] == "offline"
    assert main(["--url", base, "node", "add", "user@gpu-box", "--dry-run", "--wsl"]) == 0
    assert main(["--url", base, "job", "list", "--json"]) == 0
