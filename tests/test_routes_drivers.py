from __future__ import annotations

import json
import os

from eks_harness.drivers.workers import WorkerManager


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


def test_drivers_list_workers_and_describe_one(client, ctx) -> None:
    sid = acquire(client)
    assert client.get(f"/api/drivers/{sid}").json()["worker"] is None
    register(ctx, "web", sid, 1)
    listed = client.get("/api/drivers/workers").json()["items"]
    assert [(w["kind"], w["sid"]) for w in listed] == [("web", sid)]
    info = client.get(f"/api/drivers/{sid}").json()
    assert info["platform"] == "web" and info["worker"]["kind"] == "web"
    assert client.get("/api/drivers/nosuch").status_code == 404


def test_drivers_mobile_lease_maps_to_the_mobile_worker(client, ctx) -> None:
    sid = acquire(client, "ios")
    register(ctx, "mobile", sid, 1)
    info = client.get(f"/api/drivers/{sid}").json()
    assert info["platform"] == "ios" and info["worker"]["kind"] == "mobile"


def test_the_removed_driving_routes_are_gone(client, ctx) -> None:
    sid = acquire(client)
    register(ctx, "web", sid, 1)
    for route in ("act", "observe", "capture"):
        assert client.post(f"/api/drivers/{sid}/{route}", json={}).status_code in (404, 405)
    route = "events"
    assert client.get(f"/api/drivers/{sid}/{route}").status_code == 404
