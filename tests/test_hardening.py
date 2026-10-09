from __future__ import annotations

import asyncio
import os
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eks_harness.cli.store_cmds import safe_filename
from eks_harness.daemon.events import EventBus
from eks_harness.daemon.leases import manager
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.pools.backends import repo_cache_root
from eks_harness.store import deletion, layout
from eks_harness.store import retention as store_retention
from conftest import AuthEnv, make_app
from test_store_support import png_bytes, upload

PROJECT = "acme/web"


def acquire(client: TestClient, instance: str, kind: str, headers: dict | None = None, **extra):
    return client.post("/api/leases/acquire", params={"waitSeconds": 5}, headers=headers,
                       json={"kind": kind, "project": PROJECT, "session": "main", "instance": instance, **extra})


class TestRequestGuard:
    def test_unknown_host_is_refused(self, client: TestClient) -> None:
        response = client.get("/api/health", headers={"Host": "attacker.example:7171"})
        assert response.status_code == 421 and response.json()["error"] == "host_not_allowed"
        assert client.get("/api/health", headers={"Host": "127.0.0.1:7171"}).status_code == 200
        assert client.get("/api/health", headers={"Host": "localhost:7171"}).status_code == 200

    def test_rebinding_cannot_patch_settings(self, client: TestClient) -> None:
        response = client.patch("/api/settings", headers={"Host": "attacker.example:7171",
                                                          "Origin": "http://attacker.example:7171"},
                                json={"values": {"browser.command": "/bin/sh"}})
        assert response.status_code == 421

    def test_cross_site_unsafe_requests_are_refused(self, client: TestClient) -> None:
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site"}):
            response = client.post("/api/daemon/sweep", headers=headers, content=b"x")
            assert response.status_code == 403, headers
            assert response.json()["error"] == "cross_site_request"
        assert client.post("/api/daemon/sweep", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.post("/api/daemon/sweep", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert client.post("/api/daemon/sweep").status_code == 200

    def test_public_url_host_and_extra_hosts(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        config.set("server.publicUrl", "https://share.example.test")
        monkeypatch.setenv("EKS_HARNESS_SERVER_ALLOWEDHOSTS", '["harness.lan"]')
        config.reload()
        with TestClient(make_app(config)) as client:
            assert client.get("/api/health", headers={"Host": "share.example.test"}).status_code == 200
            assert client.get("/api/health", headers={"Host": "harness.lan:7171"}).status_code == 200
            assert client.get("/api/health", headers={"Host": "testserver"}).status_code == 421
            ok = client.post("/api/daemon/sweep", headers={"Host": "share.example.test",
                                                           "Origin": "https://share.example.test"})
            assert ok.status_code == 200

    def test_spa_shell_has_a_content_security_policy(self, client: TestClient, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html><head><script>var a = 1;</script>"
                                         "<script type=\"module\" src=\"/assets/app.js\"></script></head></html>")
        monkeypatch.setenv("EKS_HARNESS_WEB_DIST", str(dist))
        page = client.get("/p/acme/web")
        policy = page.headers["content-security-policy"]
        assert "script-src 'self' 'sha256-" in policy and "object-src 'none'" in policy
        assert "base-uri 'none'" in policy and "frame-src 'self'" in policy


class TestBackendSecrets:
    def test_members_do_not_see_exports(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        ensured = client.post("/api/backends/ensure", headers=auth_env.admin_headers,
                              json={"definition": "api", "instance": "inst-a", "hold": 60})
        backend_id = ensured.json()["id"]
        assert ensured.json()["exports"]
        _, key = auth_env.make_user("mia")
        listed = client.get("/api/backends", headers=auth_env.headers_for(key)).json()["items"][0]
        assert listed["exports"] == {} and listed["captured"] == {} and listed["redacted"] is True
        assert listed.get("tree") is None and listed.get("logFile") is None and listed.get("logs") == {}
        detail = client.get(f"/api/backends/{backend_id}", headers=auth_env.headers_for(key)).json()
        assert detail["exports"] == {}
        status = client.get("/api/status", headers=auth_env.headers_for(key)).json()
        assert status["backends"][0]["exports"] == {}
        admin_view = client.get("/api/backends", headers=auth_env.admin_headers).json()["items"][0]
        assert admin_view["exports"] and admin_view["redacted"] is False

    def test_editor_of_a_bound_lease_sees_exports(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        backend_id = client.post("/api/backends/ensure", headers=auth_env.admin_headers,
                                 json={"definition": "api", "instance": "inst-b", "hold": 60}).json()["id"]
        user, key = auth_env.make_user("eli")
        auth_env.grant(user, PROJECT, "editor")
        assert acquire(client, "inst-b", "browser", auth_env.headers_for(key), backend=backend_id).status_code == 200
        detail = client.get(f"/api/backends/{backend_id}", headers=auth_env.headers_for(key)).json()
        assert detail["exports"] and detail["redacted"] is False


class TestActivityAndStart:
    def test_device_activity_hides_other_projects(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        assert acquire(client, "admin-agent", "ios", auth_env.admin_headers).status_code == 200
        _, key = auth_env.make_user("nora")
        detail = client.get("/api/devices/ios/1", headers=auth_env.headers_for(key)).json()
        assert detail["activity"]
        assert all(item["projectId"] is None and item["leaseSid"] is None for item in detail["activity"])
        admin = client.get("/api/devices/ios/1", headers=auth_env.admin_headers).json()
        assert any(item["projectId"] == PROJECT for item in admin["activity"])

    def test_start_and_shutdown_need_grants(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        _, key = auth_env.make_user("otto")
        headers = auth_env.headers_for(key)
        assert client.post("/api/devices/ios/2/start", headers=headers, json={}).status_code == 403
        assert client.post("/api/profiles/browser:1:4/start", headers=headers, json={}).status_code == 403
        assert client.post("/api/browsers/1/start", headers=headers, json={}).status_code == 403
        assert client.post("/api/browsers/1/start", headers=auth_env.admin_headers, json={}).status_code == 200
        stop = client.post("/api/browsers/1/shutdown", headers=headers, json={"confirm": "browser:1"})
        assert stop.status_code == 403 and stop.json()["error"] == "admin_required"
        user, editor_key = auth_env.make_user("ella")
        auth_env.grant(user, PROJECT, "editor")
        started = client.post("/api/devices/ios/2/start", headers=auth_env.headers_for(editor_key), json={})
        assert started.status_code == 200


class TestLeaseStateDir:
    def test_state_dir_must_be_a_harness_state_dir(self, client: TestClient, ctx, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        refused = acquire(client, "agent-a", "ios", state_dir=str(outside))
        assert refused.status_code == 400 and refused.json()["error"] == "invalid_state_dir"
        traversal = repo_cache_root(ctx.paths) / "state" / ".." / ".." / "x"
        assert acquire(client, "agent-a", "ios", state_dir=str(traversal)).status_code == 400
        inside = repo_cache_root(ctx.paths) / "state" / "slot-1" / "mobile"
        inside.mkdir(parents=True)
        (inside / "ios-udid").write_text("X")
        granted = acquire(client, "agent-a", "ios", state_dir=str(inside))
        assert granted.status_code == 200
        client.post(f"/api/leases/{granted.json()['sid']}/release", json={})
        assert not (inside / "ios-udid").exists()


class TestTickAndBusy:
    def test_idle_shutdown_skips_a_device_granted_meanwhile(self, client: TestClient, ctx) -> None:
        leases = manager(ctx)
        first = acquire(client, "agent-a", "ios").json()
        client.post(f"/api/leases/{first['sid']}/release", json={})
        key = first["resource"]
        ctx.pools.host.devices[key]["last_used"] = 0
        ctx.config.set("devices.idleSeconds", 1)
        assert leases.claim_idle({key}, "idle shutdown", device=key) is True
        assert ctx.pools.host.devices[key]["busy"] == "idle shutdown"
        leases.clear_busy({key})
        second = acquire(client, "agent-b", "ios").json()
        assert second["resource"] == key
        assert leases.claim_idle({key}, "idle shutdown", device=key) is False
        report = leases.tick()
        assert key not in report["idleDevices"]
        assert ctx.pools.host.devices[key]["status"] == "on"

    def test_prepare_waits_for_a_busy_resource(self, client: TestClient, ctx) -> None:
        leases = manager(ctx)
        granted = acquire(client, "agent-c", "ios").json()
        lease = leases.get_lease(granted["sid"])
        leases.mark_busy({lease.resource}, "reset")
        thread = threading.Thread(target=leases.prepare, args=(lease.id,))
        thread.start()
        time.sleep(0.3)
        assert thread.is_alive()
        leases.clear_busy({lease.resource})
        thread.join(5)
        assert not thread.is_alive()
        assert leases.get_lease(granted["sid"]).phase == "ready"

    def test_prepare_gives_up_when_the_lease_ends_while_busy(self, client: TestClient, ctx) -> None:
        leases = manager(ctx)
        granted = acquire(client, "agent-d", "ios").json()
        lease = leases.get_lease(granted["sid"])
        leases.mark_busy({lease.resource}, "shutdown")
        thread = threading.Thread(target=leases.prepare, args=(lease.id,))
        thread.start()
        leases.end_leases([lease], "broken", "test")
        thread.join(5)
        assert not thread.is_alive()
        leases.clear_busy({lease.resource})


class TestRecordingReset:
    def test_video_reset_clears_the_recorder(self, client: TestClient, ctx) -> None:
        lease = acquire(client, "agent-v", "android").json()
        sid = lease["sid"]
        assert client.post(f"/api/captures/{sid}/video/start", json={}).status_code == 200
        reset = client.post(f"/api/captures/{sid}/video/reset", json={})
        assert reset.status_code == 200 and reset.json()["stoppedRecorder"] is True
        assert not ctx.pools.devices.is_recording(lease["resource"])
        assert "recordingSid" not in ctx.pools.host.devices[lease["resource"]]
        assert client.post(f"/api/captures/{sid}/video/start", json={}).status_code == 200

    def test_capture_dirs_are_collected(self, client: TestClient, ctx) -> None:
        stale = ctx.paths.tmp_dir / "capture-deadbeef"
        stale.mkdir(parents=True)
        (stale / "x.png").write_bytes(b"x")
        old = time.time() - 7 * 3600
        os.utime(stale, (old, old))
        assert store_retention.cleanup_tmp(ctx.paths) == 1
        assert not stale.exists()


class TestStoreRaces:
    def test_retention_skips_an_artifact_pinned_meanwhile(self, client: TestClient, ctx) -> None:
        body = upload(client, png_bytes(), "a.png", "image/png", project=PROJECT).json()
        with ctx.db.transaction() as conn:
            conn.execute("UPDATE artifacts SET created_at = 1 WHERE id = ?", (body["id"],))
        client.patch(f"/api/artifacts/{body['id']}", json={"pinned": True})
        plan = deletion.delete_artifacts(ctx.db, ctx.paths, [body["id"]], unpinned_before=time.time())
        assert plan.artifact_ids == []
        assert artifacts_repo.get(ctx.db.conn(), body["id"]) is not None

    def test_place_dir_recreates_a_parent_removed_by_a_delete(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "f").write_text("x")
        target = store / "o" / "n" / "s" / "A" / "dir"
        target.parent.mkdir(parents=True)
        layout.remove_empty_parents(target.parent, store)
        layout.place_dir(staged, target)
        assert (target / "f").read_text() == "x"


class TestEventStream:
    def test_overflow_ends_the_stream(self, db) -> None:
        bus = EventBus(db)

        async def run() -> list[bytes]:
            chunks: list[bytes] = []
            stream = bus.sse(None, heartbeat_seconds=0.2)
            chunks.append(await stream.__anext__())
            subscription = bus._subscribers[-1]
            subscription.overflowed = True
            async for chunk in stream:
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(asyncio.wait_for(run(), 5))
        assert chunks == [b"retry: 3000\n\n"]

    def test_refresh_closes_a_revoked_stream(self, db) -> None:
        bus = EventBus(db)

        async def run() -> int:
            count = 0
            async for _ in bus.sse(None, heartbeat_seconds=0.05, refresh=lambda: None, refresh_seconds=0.1):
                count += 1
            return count

        assert asyncio.run(asyncio.wait_for(run(), 5)) >= 1


class TestFiles:
    def test_database_and_dirs_are_private(self, ctx) -> None:
        mode = stat.S_IMODE(os.stat(ctx.paths.db_file).st_mode)
        assert mode & 0o077 == 0
        for directory in ctx.paths.all_dirs():
            assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0

    def test_download_names_are_basenames(self) -> None:
        assert safe_filename("../../.zshrc", "x") == ".zshrc"
        assert safe_filename("/etc/passwd", "x") == "passwd"
        assert safe_filename("..\\..\\evil.bat", "x") == "evil.bat"
        assert safe_filename("..", "fallback.bin") == "fallback.bin"
        assert safe_filename("", "fallback.bin") == "fallback.bin"
