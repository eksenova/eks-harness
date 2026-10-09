from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from eks_harness.config import Config
from eks_harness.daemon.leases import manager
from eks_harness.db.repos import events as events_repo
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.ids import is_sid

from conftest import AuthEnv, make_app

PROJECT = "acme/web-app"


def acquire(client: TestClient, instance: str = "session:one", kind: str = "browser", project: str = PROJECT,
            session: str = "feature/yardim-merkezi", wait: bool = True, headers: dict | None = None, **extra):
    body = {"kind": kind, "project": project, "session": session, "instance": instance, "wait": wait, **extra}
    return client.post("/api/leases/acquire", json=body, params={"waitSeconds": 5 if wait else 0}, headers=headers)


def granted(client: TestClient, **kwargs) -> dict:
    response = acquire(client, **kwargs)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "granted", data
    return data


def test_acquire_creates_project_session_and_sid(client: TestClient, ctx) -> None:
    data = granted(client)
    sid = data["sid"]
    assert is_sid(sid)
    assert data["lease"]["state"] == "active" and data["lease"]["phase"] == "ready"
    assert data["resource"] == "browser:1:1"
    assert data["lease"]["cdp"] == "fake://browser/1"
    assert data["project"]["id"] == PROJECT
    assert data["session"]["name"] == "feature/yardim-merkezi"
    assert data["session"]["slug"] == "feature-yardim-merkezi"
    assert data["urls"]["session"].endswith("/p/acme/web-app/s/feature-yardim-merkezi")
    project = projects_repo.get(ctx.db.conn(), PROJECT)
    assert project is not None and project.implicit
    again = granted(client)
    assert again["sid"] == sid
    types = [e.type for e in events_repo.list_events(ctx.db.conn(), lease_sid=sid, ascending=True)]
    assert types[:3] == ["lease.queued", "lease.acquired", "lease.ready"]
    assert all(e.session_id == data["session"]["id"] for e in events_repo.list_events(ctx.db.conn(), lease_sid=sid))


def test_released_sid_is_gone_and_resume_reuses_the_session(client: TestClient) -> None:
    first = granted(client)
    sid = first["sid"]
    assert client.post(f"/api/leases/{sid}/heartbeat", json={}).status_code == 200
    released = client.post(f"/api/leases/{sid}/release", json={"reason": "done"})
    assert released.status_code == 200
    assert released.json()["sids"] == [sid]
    gone = client.post(f"/api/leases/{sid}/heartbeat", json={})
    assert gone.status_code == 410
    payload = gone.json()
    assert payload["error"] == "lease_released"
    assert payload["reacquire"] == (f"eks-harness lease acquire --project {PROJECT} --session "
                                    f"feature/yardim-merkezi --kind browser")
    history = client.get(f"/api/leases/{sid}")
    assert history.status_code == 200 and history.json()["state"] == "released"
    assert client.post(f"/api/leases/{sid}/release", json={}).json()["sids"] == []
    resumed = client.post("/api/leases/resume", json={"sid": sid}, params={"waitSeconds": 5})
    assert resumed.status_code == 200, resumed.text
    data = resumed.json()
    assert data["status"] == "granted"
    assert data["sid"] != sid
    assert data["lease"]["previousSid"] == sid
    assert data["session"]["id"] == first["session"]["id"]
    assert data["lease"]["ownerInstance"] == "session:one"


def test_resume_of_a_live_sid_returns_it(client: TestClient) -> None:
    sid = granted(client)["sid"]
    data = client.post("/api/leases/resume", json={"sid": sid}).json()
    assert data["sid"] == sid and data["status"] == "granted"


def test_fifo_queue_per_kind(paths) -> None:
    config = Config(paths)
    config.set_many({"browser.instances": 1, "browser.profilesPerInstance": 1})
    with TestClient(make_app(config)) as client:
        holder = granted(client, instance="a")
        second = acquire(client, instance="b", wait=False).json()
        third = acquire(client, instance="c", wait=False).json()
        assert second["status"] == "queued" and second["position"] == 1 and second["waiting"] == 1
        assert third["position"] == 2 and third["waiting"] == 2
        assert second["holders"][0]["sid"] == holder["sid"]
        assert granted(client, instance="x", kind="ios")["resource"] == "ios:1"
        client.post(f"/api/leases/{holder['sid']}/release", json={})
        again = acquire(client, instance="b", wait=True).json()
        assert again["status"] == "granted" and again["sid"] == second["sid"]
        still = acquire(client, instance="c", wait=False).json()
        assert still["status"] == "queued" and still["position"] == 1
        listing = client.get("/api/leases").json()
        assert [q["sid"] for q in listing["queue"]] == [third["sid"]]
        assert listing["queue"][0]["queuePosition"] == 1


def test_queue_waiter_expires_without_polls(paths) -> None:
    config = Config(paths)
    config.set_many({"browser.instances": 1, "browser.profilesPerInstance": 1, "lease.queueTimeoutSeconds": 5})
    app = make_app(config)
    with TestClient(app) as client:
        granted(client, instance="a")
        waiting = acquire(client, instance="b", wait=False).json()
        ctx = app.state.ctx
        with ctx.db.transaction() as conn:
            leases_repo.update(conn, leases_repo.get_by_sid(conn, waiting["sid"]).id, last_poll_at=time.time() - 60)
        report = manager(ctx).tick()
        assert waiting["sid"] in report["expiredWaiters"]
        lease = client.get(f"/api/leases/{waiting['sid']}").json()
        assert lease["state"] == "released" and lease["reason"].startswith("stopped waiting")


def test_stale_and_idle_leases_are_released_by_the_tick(app, client: TestClient) -> None:
    ctx = app.state.ctx
    stale = granted(client, instance="stale")
    idle = granted(client, instance="idle", kind="android")
    fresh = granted(client, instance="fresh", kind="ios")
    with ctx.db.transaction() as conn:
        leases_repo.update(conn, leases_repo.get_by_sid(conn, stale["sid"]).id, heartbeat_at=time.time() - 3600)
    response = client.post("/api/instances/idle/idle", json={"grace": 0})
    assert response.status_code == 200 and response.json()["sids"] == [idle["sid"]]
    assert client.get(f"/api/leases/{idle['sid']}").json()["state"] == "idle"
    time.sleep(0.05)
    report = manager(ctx).tick()
    reasons = {item["sid"]: item["reason"] for item in report["stale"]}
    assert reasons[stale["sid"]].startswith("no activity for")
    assert idle["sid"] in reasons
    assert fresh["sid"] not in reasons
    assert client.post(f"/api/leases/{fresh['sid']}/heartbeat", json={}).status_code == 200
    assert client.post(f"/api/leases/{stale['sid']}/heartbeat", json={}).status_code == 410


def test_heartbeat_clears_idle(client: TestClient) -> None:
    sid = granted(client)["sid"]
    assert client.post(f"/api/leases/{sid}/idle", json={"grace": 30}).json()["state"] == "idle"
    beat = client.post(f"/api/leases/{sid}/heartbeat", json={"meta": {"browserContextIds": ["ctx-1"]}}).json()
    assert beat["state"] == "active"
    lease = client.get(f"/api/leases/{sid}").json()
    assert lease["idleLimit"] is None
    assert lease["meta"]["browserContextIds"] == ["ctx-1"]


def test_instance_heartbeat_release_and_ended(client: TestClient, ctx) -> None:
    browser = granted(client, instance="tree:/work/a")
    device = granted(client, instance="tree:/work/a", kind="ios")
    beat = client.post("/api/instances/tree:/work/a/heartbeat", json={}).json()
    assert sorted(beat["leases"]) == ["browser", "ios"]
    released = client.post("/api/instances/tree:/work/a/release", json={"kind": "ios"}).json()
    assert released["sids"] == [device["sid"]]
    ensured = client.post("/api/backends/ensure", json={"definition": "api", "instance": "tree:/work/a"})
    assert ensured.status_code == 200 and ensured.json()["status"] == "running"
    ended = client.post("/api/instances/tree:/work/a/ended", json={}).json()
    assert ended["sids"] == [browser["sid"]]
    assert ctx.pools.backends.get("api@tree:/work/a") is None


def test_break_records_the_reason(client: TestClient) -> None:
    sid = granted(client)["sid"]
    response = client.post("/api/leases/break", json={"sid": sid, "reason": "stuck"})
    assert response.status_code == 200 and response.json()["sids"] == [sid]
    gone = client.post(f"/api/leases/{sid}/heartbeat", json={})
    assert gone.status_code == 410 and gone.json()["state"] == "broken"
    assert client.get(f"/api/leases/{sid}").json()["reason"] == "stuck"


def test_failed_preparation_is_reported_and_released(app, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = app.state.ctx

    def broken_boot(key: str) -> None:
        raise RuntimeError("simulator runtime missing")

    monkeypatch.setattr(ctx.pools.devices, "boot", broken_boot)
    data = acquire(client, kind="ios").json()
    assert data["status"] == "failed"
    assert "simulator runtime missing" in data["error"]
    report = manager(ctx).tick()
    assert any(item["sid"] == data["sid"] and item["reason"] == "preparation failed" for item in report["stale"])


def test_device_captures_create_artifacts_and_fail_after_release(client: TestClient) -> None:
    lease = granted(client, kind="ios", project="acme/mobile-app", session="main")
    sid = lease["sid"]
    shot = client.post(f"/api/captures/{sid}/screenshot", json={"caption": "home", "tags": ["smoke"]})
    assert shot.status_code == 200, shot.text
    artifact = shot.json()
    assert artifact["kind"] == "screenshot" and artifact["caption"] == "home"
    assert artifact["url"].endswith(f"/p/acme/mobile-app/s/main/a/{artifact['id']}")
    assert artifact["rawUrl"] and artifact["downloadUrl"] and artifact["sessionUrl"]
    assert artifact["meta"]["device"] == "Harness iOS 1" and artifact["leaseSid"] == sid
    started = client.post(f"/api/captures/{sid}/video/start", json={})
    assert started.status_code == 200 and started.json()["recording"] is True
    assert client.post(f"/api/captures/{sid}/video/start", json={}).status_code == 409
    video = client.post(f"/api/captures/{sid}/video/stop", json={"caption": "flow"})
    assert video.status_code == 200, video.text
    assert video.json()["kind"] == "video"
    log = client.post(f"/api/captures/{sid}/log", json={}, params={"since": -60})
    assert log.status_code == 200 and log.json()["kind"] == "log"
    client.post(f"/api/leases/{sid}/release", json={})
    gone = client.post(f"/api/captures/{sid}/screenshot", json={})
    assert gone.status_code == 410 and gone.json()["error"] == "lease_released"


def test_browser_captures_are_not_made_by_the_daemon(client: TestClient) -> None:
    sid = granted(client)["sid"]
    response = client.post(f"/api/captures/{sid}/screenshot", json={})
    assert response.status_code == 400 and response.json()["error"] == "browser_capture"


def test_releasing_a_recording_lease_discards_the_video(app, client: TestClient) -> None:
    ctx = app.state.ctx
    sid = granted(client, kind="android", session="main")["sid"]
    assert client.post(f"/api/captures/{sid}/video/start", json={}).status_code == 200
    assert ctx.pools.devices.is_recording("android:1")
    client.post(f"/api/leases/{sid}/release", json={})
    assert not ctx.pools.devices.is_recording("android:1")


def test_graceful_restart_reloads_leases_and_pools(paths) -> None:
    config = Config(paths)
    first = make_app(config)
    with TestClient(first) as client:
        browser = granted(client, instance="restart")
        device = granted(client, instance="restart", kind="android")
        ensured = client.post("/api/backends/ensure", json={"definition": "api", "instance": "restart"}).json()
        assert ensured["status"] == "running"
    second = make_app(Config(paths))
    with TestClient(second) as client:
        lease = client.get(f"/api/leases/{browser['sid']}").json()
        assert lease["state"] == "active" and lease["phase"] == "ready"
        assert lease["cdp"] == "fake://browser/1"
        assert client.post(f"/api/leases/{browser['sid']}/heartbeat", json={}).status_code == 200
        assert client.post(f"/api/leases/{device['sid']}/heartbeat", json={}).status_code == 200
        devices = {d["key"]: d for d in client.get("/api/devices").json()["items"]}
        assert devices["android:1"]["status"] == "on"
        assert devices["android:1"]["lease"]["sid"] == device["sid"]
        backend = client.get("/api/backends/api@restart").json()
        assert backend["status"] == "running"
        assert backend["bindings"] == ["hold ensure"]
        again = granted(client, instance="restart")
        assert again["sid"] == browser["sid"]


def test_preparing_leases_are_resumed_after_restart(paths) -> None:
    config = Config(paths)
    app = make_app(config)
    with TestClient(app) as client:
        sid = granted(client, instance="p", kind="ios")["sid"]
        with app.state.ctx.db.transaction() as conn:
            leases_repo.update(conn, leases_repo.get_by_sid(conn, sid).id, phase="preparing")
    with TestClient(make_app(Config(paths))) as client:
        deadline = time.time() + 5
        while time.time() < deadline and client.get(f"/api/leases/{sid}").json()["phase"] != "ready":
            time.sleep(0.05)
        assert client.get(f"/api/leases/{sid}").json()["phase"] == "ready"


def test_session_move_keeps_the_sid(client: TestClient, ctx) -> None:
    first = granted(client, session="one")
    second = granted(client, session="two")
    assert second["sid"] == first["sid"]
    assert second["session"]["slug"] == "two"
    session = sessions_repo.find_in_project(ctx.db.conn(), PROJECT, "two")
    assert leases_repo.get_by_sid(ctx.db.conn(), first["sid"]).session_id == session.id


def test_lease_grants(auth_env: AuthEnv) -> None:
    client = auth_env.client
    admin = auth_env.admin_headers
    owned = acquire(client, headers=admin, instance="admin").json()
    assert owned["status"] == "granted"
    member, key = auth_env.make_user("mia")
    headers = auth_env.headers_for(key)
    assert acquire(client, headers=headers, instance="mia", project="other/project").status_code == 403
    assert acquire(client, headers=headers, instance="mia").status_code == 404
    assert client.post(f"/api/leases/{owned['sid']}/heartbeat", json={}, headers=headers).status_code == 404
    auth_env.grant(member, PROJECT, "viewer")
    assert acquire(client, headers=headers, instance="mia").status_code == 403
    assert client.get(f"/api/leases/{owned['sid']}", headers=headers).status_code == 200
    auth_env.grant(member, PROJECT, "editor")
    mine = acquire(client, headers=headers, instance="mia")
    assert mine.status_code == 200 and mine.json()["status"] == "granted"
    assert client.post(f"/api/leases/{owned['sid']}/heartbeat", json={}, headers=headers).status_code == 200
    assert client.post("/api/leases/acquire", json={"kind": "browser", "project": PROJECT, "session": "x",
                                                    "instance": "anon"}).status_code == 401


def test_backend_binding_is_reported_by_leases(client: TestClient, ctx) -> None:
    backend = client.post("/api/backends/ensure", json={"definition": "api", "instance": "bind", "hold": 0}).json()
    lease = granted(client, instance="bind", backend=backend["id"])
    detail = client.get(f"/api/backends/{backend['id']}").json()
    assert detail["bindings"] == ["lease browser:1:1"]
    client.post(f"/api/leases/{lease['sid']}/release", json={})
    assert client.get(f"/api/backends/{backend['id']}").json()["bindings"] == []
