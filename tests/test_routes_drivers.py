from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from eks_harness.drivers.workers import WorkerManager


class FakeWorker(BaseHTTPRequestHandler):
    scripts: list[str] = []

    def log_message(self, *args) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/exec":
            FakeWorker.scripts.append(body["script"])
            if "explode" in body["script"]:
                self._send(200, {"ok": False, "error": "no such target"})
            else:
                self._send(200, {"ok": True, "value": {"echo": body["script"]}})
        else:
            self._send(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"name": "route-changed", "data": {"route": "/home"}}\n\n')
            self.wfile.flush()
            return
        self._send(200, {"ok": True})


@pytest.fixture
def worker(ctx):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWorker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeWorker.scripts = []
    yield server
    server.shutdown()


def register(ctx, kind: str, sid: str, port: int) -> None:
    manager = WorkerManager(ctx.paths)
    folder = manager.state_dir(kind, sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "record.json").write_text(json.dumps({
        "kind": kind, "key": sid, "pid": os.getpid(), "port": port, "stateDir": str(folder),
        "log": str(folder / "worker.log"), "code": "x", "configHash": "y", "startedAt": 1.0}), encoding="utf-8")


def acquire(client, kind: str = "browser") -> str:
    response = client.post("/api/leases/acquire", json={"kind": kind, "project": "acme/web", "session": "main",
                                                        "instance": "session:t"}, params={"waitSeconds": 5})
    assert response.status_code == 200, response.text
    return response.json()["sid"]


def test_drivers_act_observe_capture_and_events(client, ctx, worker) -> None:
    sid = acquire(client)
    assert client.post(f"/api/drivers/{sid}/act", json={"action": "press", "params": {"target": "#go"}}).status_code == 404
    register(ctx, "web", sid, worker.server_address[1])
    listed = client.get("/api/drivers/workers").json()["items"]
    assert [(w["kind"], w["sid"]) for w in listed] == [("web", sid)]
    info = client.get(f"/api/drivers/{sid}").json()
    assert info["platform"] == "web" and info["worker"]["kind"] == "web"
    acted = client.post(f"/api/drivers/{sid}/act", json={"action": "press", "params": {"target": "#go"}})
    assert acted.status_code == 200, acted.text
    assert acted.json()["action"] == "press" and 'click("#go")' in FakeWorker.scripts[-1]
    observed = client.post(f"/api/drivers/{sid}/observe", json={"query": "text", "params": {"target": "h1"}}).json()
    assert observed["query"] == "text" and 'text("h1")' in FakeWorker.scripts[-1]
    shot = client.post(f"/api/drivers/{sid}/capture", json={"kind": "screenshot", "params": {"name": "home"}}).json()
    assert shot["kind"] == "screenshot" and 'shot("home")' in FakeWorker.scripts[-1]
    failed = client.post(f"/api/drivers/{sid}/act", json={"action": "explode"})
    assert failed.status_code == 400 and "no such target" in failed.text
    assert client.post(f"/api/drivers/{sid}/act", json={}).status_code == 400
    with client.stream("GET", f"/api/drivers/{sid}/events") as stream:
        lines = []
        for line in stream.iter_lines():
            lines.append(line)
            if line.startswith("data:"):
                break
    assert any("route-changed" in line for line in lines)


def test_drivers_mobile_lease_uses_the_mobile_worker(client, ctx, worker) -> None:
    sid = acquire(client, "ios")
    register(ctx, "mobile", sid, worker.server_address[1])
    acted = client.post(f"/api/drivers/{sid}/act", json={"action": "navigate", "params": {"route": "Home"}})
    assert acted.status_code == 200, acted.text
    assert 'navigate("Home")' in FakeWorker.scripts[-1]
    assert client.get("/api/drivers/nosuch").status_code == 404
