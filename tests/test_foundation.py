from __future__ import annotations

import asyncio
import json
import os
import stat
import struct
import zlib
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from eks_harness._version import compute_source_hash, iter_source_files
from eks_harness.api.deps import (
    ProjectAccess,
    SessionAccess,
    current_principal,
    event_filter_for,
    require_admin,
    require_project,
    require_session,
)
from eks_harness.api.schemas import ArtifactOut, LeaseAcquireRequest, ShareCreate
from eks_harness.auth import core as auth_core
from eks_harness.auth import keys as auth_keys
from eks_harness.cli import main as cli_main
from eks_harness.cli import credentials
from eks_harness.cli.client import Gone, HarnessClient, NotAuthenticated
from eks_harness.config import Config, ConfigError, env_name
from eks_harness.daemon.events import EventBus, format_sse
from eks_harness.db import open_database
from eks_harness.db.migrations import current_version, migrate
from eks_harness.db.repos import artifacts, events, leases, projects, sessions, users
from eks_harness.ids import is_sid, is_ulid, new_sid, new_ulid, parse_project_id, slugify
from eks_harness.paths import resolve_paths
from eks_harness.pools import create_pools
from eks_harness.store import search as store_search

from conftest import add_grant, bearer, create_key, create_user, ensure_session, make_app

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_health_and_unknown_api(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    missing = client.get("/api/does-not-exist")
    assert missing.status_code == 404
    assert set(missing.json()) >= {"error", "message"}


def test_spa_fallback_without_build(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EKS_HARNESS_WEB_DIST", str(tmp_path / "nothing"))
    assert client.get("/p/a/b").status_code == 503
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("EKS_HARNESS_WEB_DIST", str(dist))
    page = client.get("/p/a/b/s/c")
    assert page.status_code == 200 and "<title>x</title>" in page.text
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/../../etc/passwd").status_code in (200, 404)


def test_migrations_apply_twice(paths) -> None:
    database = open_database(paths.db_file)
    conn = database.conn()
    first = current_version(conn)
    assert first >= 1
    assert migrate(conn) == []
    assert migrate(conn) == []
    assert current_version(conn) == first
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert users.local_user(conn).role == "admin"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    for name in ("projects", "sessions", "leases", "artifacts", "artifact_tags", "artifacts_fts", "notes", "users",
                 "api_keys", "grants", "web_sessions", "shares", "seen", "events", "settings_audit"):
        assert name in tables
    database.close()
    again = open_database(paths.db_file)
    assert current_version(again.conn()) == first
    again.close()


def test_repositories_and_fts(db) -> None:
    session = ensure_session(db, "acme/web-app", "feature/yardim-merkezi")
    assert session.slug == "feature-yardim-merkezi"
    with db.transaction() as conn:
        same, created = sessions.ensure_in_project(conn, "acme/web-app", "feature/yardim-merkezi")
        assert not created and same.id == session.id
        artifact = artifacts.insert(conn, artifact_id=new_ulid(), project_id="acme/web-app",
                                    session_id=session.id, kind="screenshot", filename="login-page.png",
                                    rel_path="x/login-page.png", mime="image/png", size=10, caption="Login form",
                                    tags=["Evidence", "login"])
    assert artifact.tags == ("evidence", "login")
    hits = store_search.search(db.conn(), "yardim")
    assert [h.artifact.id for h in hits] == [artifact.id]
    assert store_search.search(db.conn(), "evidence")[0].artifact.id == artifact.id
    with db.transaction() as conn:
        sessions.rename(conn, session.id, "release candidate")
        artifacts.set_tags(conn, artifact.id, ["final"])
    assert store_search.search(db.conn(), "candidate")[0].artifact.id == artifact.id
    assert store_search.search(db.conn(), "evidence") == []
    items, cursor = artifacts.query(db.conn(), artifacts.ArtifactQuery(project_id="acme/web-app", limit=10))
    assert [i.id for i in items] == [artifact.id] and cursor is None
    stats = projects.stats(db.conn(), "acme/web-app", user_id=users.local_user(db.conn()).id)
    assert stats.artifact_count == 1 and stats.unseen_count == 1 and stats.session_count == 1
    with db.transaction() as conn:
        projects.delete(conn, "acme/web-app")
    assert artifacts.get(db.conn(), artifact.id) is None
    assert store_search.search(db.conn(), "candidate") == []


def test_leases_repo(db) -> None:
    session = ensure_session(db, "acme/mobile-app", "main")
    with db.transaction() as conn:
        lease = leases.insert(conn, kind="ios", owner_instance="session:abc", session_id=session.id)
        assert lease.state == "queued" and is_sid(lease.sid)
        lease = leases.activate(conn, lease.id, "ios:1")
        assert lease.holding and lease.project_id == "acme/mobile-app"
        assert leases.taken_resources(conn) == {"ios:1"}
        ended = leases.end(conn, lease.id, "released", "done")
        assert ended.ended


def test_ids() -> None:
    assert is_ulid(new_ulid())
    assert is_sid(new_sid())
    assert slugify("Feature/Yardım Merkezi") == "feature-yardim-merkezi"
    assert slugify("_project") == "project"
    assert parse_project_id("acme/web-app") == ("acme", "web-app")
    with pytest.raises(ValueError):
        parse_project_id("Bad/Name")


def test_config_roundtrip(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(paths)
    assert cfg["server.port"] == 7171 and cfg["live.idleStopSeconds"] == 10 and cfg["retention.defaultDays"] is None
    cfg.set("server.port", "7272")
    cfg.set("server.trustedProxies", "10.0.0.0/8, 192.168.1.0/24")
    cfg.set("retention.defaultDays", "")
    assert Config(paths)["server.port"] == 7272
    assert Config(paths)["server.trustedProxies"] == ["10.0.0.0/8", "192.168.1.0/24"]
    with pytest.raises(ConfigError):
        cfg.set("server.trustedProxies", ["nope"])
    with pytest.raises(ConfigError):
        cfg.set("no.such", 1)
    monkeypatch.setenv(env_name("server.port"), "7373")
    reloaded = Config(paths)
    assert reloaded["server.port"] == 7373 and reloaded.source("server.port") == "env"
    cfg.unset("server.port")
    assert "server.port" not in json.loads(paths.config_file.read_text())


def test_paths_isolated(harness_home: Path) -> None:
    resolved = resolve_paths()
    assert all(str(d).startswith(str(harness_home)) for d in resolved.all_dirs())


def test_event_bus_filter_and_sse(db) -> None:
    bus = EventBus(db)

    async def scenario() -> list[bytes]:
        visible = bus.subscribe(lambda e: e.project_id == "a/b")
        bus.publish("artifact.created", project_id="x/y", detail={"n": 1})
        bus.publish("artifact.created", project_id="a/b", detail={"n": 2})
        event = await visible.next(timeout=1)
        assert event is not None and event.detail == {"n": 2}
        assert await visible.next(timeout=0.05) is None
        visible.close()
        chunks = []
        stream = bus.sse(lambda e: True, last_event_id=0, heartbeat_seconds=0.05)
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) >= 3:
                break
        await stream.aclose()
        return chunks

    chunks = asyncio.run(scenario())
    assert chunks[0].startswith(b"retry:")
    assert b"event: artifact" in chunks[1]
    assert bus.subscriber_count == 0
    assert len(events.list_events(db.conn())) == 2
    assert format_sse(bus.history()[0]).endswith(b"\n\n")


def test_local_principal_when_auth_disabled(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/api/_test/me")
    def me(principal=Depends(current_principal)) -> dict:
        return {"user": principal.username, "via": principal.via, "admin": principal.is_admin}

    app.router.routes[:0] = router.routes
    with TestClient(app) as test_client:
        assert test_client.get("/api/_test/me").json() == {"user": "local", "via": "local", "admin": True}


def _grant_routes(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/api/_test/p/{owner}/{name}")
    def view_project(access: ProjectAccess = Depends(require_project("viewer"))) -> dict:
        return {"level": access.level, "limited": access.limited}

    @router.post("/api/_test/p/{owner}/{name}")
    def edit_project(access: ProjectAccess = Depends(require_project("editor"))) -> dict:
        return {"level": access.level}

    @router.get("/api/_test/p/{owner}/{name}/s/{slug}")
    def view_session(access: SessionAccess = Depends(require_session("viewer"))) -> dict:
        return {"level": access.level, "session": access.session.slug}

    @router.post("/api/_test/p/{owner}/{name}/s/{slug}")
    def edit_session(access: SessionAccess = Depends(require_session("editor"))) -> dict:
        return {"level": access.level}

    @router.get("/api/_test/admin")
    def admin_only(principal=Depends(require_admin)) -> dict:
        return {"ok": True}

    app.router.routes[:0] = router.routes


def test_auth_grants_keys_cookies(auth_env) -> None:
    _grant_routes(auth_env.app)
    client = auth_env.client
    ensure_session(auth_env.db, "team/app", "main")
    ensure_session(auth_env.db, "team/app", "other")
    assert client.get("/api/_test/admin").status_code == 401
    assert client.get("/api/_test/admin", headers=bearer("ehk_bad")).status_code == 401
    assert client.get("/api/_test/admin", headers=auth_env.admin_headers).status_code == 200
    assert client.get("/api/health").status_code == 200

    member, key = auth_env.make_user("mia")
    headers = bearer(key)
    assert client.get("/api/_test/admin", headers=headers).status_code == 403
    assert client.get("/api/_test/p/team/app", headers=headers).status_code == 404

    auth_env.grant(member, "team/app", "viewer", session_name="main")
    assert client.get("/api/_test/p/team/app", headers=headers).json() == {"level": "viewer", "limited": True}
    assert client.get("/api/_test/p/team/app/s/main", headers=headers).status_code == 200
    assert client.get("/api/_test/p/team/app/s/other", headers=headers).status_code == 404
    assert client.post("/api/_test/p/team/app/s/main", headers=headers).status_code == 403
    assert client.post("/api/_test/p/team/app", headers=headers).status_code == 403

    auth_env.grant(member, "team/app", "editor")
    assert client.post("/api/_test/p/team/app", headers=headers).json() == {"level": "editor"}
    assert client.post("/api/_test/p/team/app/s/other", headers=headers).status_code == 200

    token, session = auth_core.create_web_session(auth_env.db, member.id, 1, "127.0.0.1", "pytest")
    client.cookies.set(auth_core.COOKIE_NAME, token)
    assert client.get("/api/_test/p/team/app").status_code == 200
    assert client.post("/api/_test/p/team/app").status_code == 403
    assert client.post("/api/_test/p/team/app", headers={auth_core.CSRF_HEADER: "wrong"}).status_code == 403
    assert client.post("/api/_test/p/team/app", headers={auth_core.CSRF_HEADER: session.csrf}).status_code == 200
    client.cookies.clear()

    prefix = key.split("_")[1]
    from eks_harness.db.repos import api_keys

    record = api_keys.get_by_prefix(auth_env.db.conn(), prefix)
    api_keys.revoke(auth_env.db.conn(), record.id)
    assert client.get("/api/_test/p/team/app", headers=headers).status_code == 401

    scope_filter = event_filter_for(auth_env.db, auth_core.Principal(member.id, "mia", "member", "key"))
    bus = auth_env.app.state.ctx.events
    visible = bus.publish("artifact.created", project_id="team/app")
    hidden = bus.publish("artifact.created", project_id="secret/app")
    device = bus.publish("device.status", resource="ios:1")
    assert scope_filter(visible) and not scope_filter(hidden) and scope_filter(device)


def test_password_hashing() -> None:
    hashed = auth_core.hash_password("correct horse battery")
    assert auth_core.verify_password(hashed, "correct horse battery")
    assert not auth_core.verify_password(hashed, "wrong")
    assert auth_keys.parse_api_key("ehk_abcdefgh_" + "a" * 32) == ("abcdefgh", "a" * 32)


def _png_dimensions(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    length = struct.unpack(">I", data[8:12])[0]
    body = data[16:16 + length]
    assert zlib.crc32(data[12:16 + length]) & 0xFFFFFFFF == struct.unpack(">I", data[16 + length:20 + length])[0]
    return struct.unpack(">II", body[:8])


def test_fake_pools(config: Config, tmp_path: Path) -> None:
    pools = create_pools(config, config.paths, fake=True)
    assert pools.fake and set(pools.host.devices) == {"ios:1", "ios:2", "ios:3", "android:1", "android:2", "android:3"}
    key = pools.devices.allocate("ios", set())
    pools.devices.boot(key)
    shot = pools.devices.screenshot(key, tmp_path / "shot.png")
    assert _png_dimensions((tmp_path / "shot.png").read_bytes()) == (shot["width"], shot["height"])
    pools.devices.record_start(key, tmp_path / "video.mp4")
    video = pools.devices.record_stop(key)
    assert (tmp_path / "video.mp4").read_bytes()[4:8] == b"ftyp" and video["durationMs"] > 0
    source = pools.devices.live_source(key)
    frame = next(iter(source.frames()))
    source.close()
    assert frame[:2] == b"\xff\xd8"
    resource = pools.browsers.allocate(set())
    assert resource == "browser:1:1"
    assert pools.browsers.ensure(1).startswith("fake://")
    pools.host.binding_source = lambda backend_id: ["lease browser:1:1"]
    record = pools.backends.ensure({"definition": "api", "instance": "session:x"})
    assert record["status"] == "running" and record["ports"]["api"] >= 5400 and record["bindings"]
    pools.backends.stop(record["id"], final=True, reason="test")
    assert pools.backends.get(record["id"]) is None
    pools.host.save()
    assert json.loads(config.paths.pools_state_file.read_text())["fake"] is True


def test_source_hash_is_stable_and_excludes_tests() -> None:
    files = iter_source_files(PACKAGE_ROOT)
    assert "pyproject.toml" in files and "src/eks_harness/_version.py" in files
    assert not any(f.startswith("tests/") or "__pycache__" in f or f.endswith("_build.json") for f in files)
    assert compute_source_hash(PACKAGE_ROOT) == compute_source_hash(PACKAGE_ROOT)


def test_schemas_contract() -> None:
    request = LeaseAcquireRequest.model_validate({"kind": "chrome", "project": "a/b", "session": "main",
                                                 "instance": "x"})
    assert request.kind == "browser" and request.wait is True
    assert ShareCreate(expires="7d").expires == "7d"
    with pytest.raises(ValueError):
        ShareCreate(expires="soon")
    dumped = ArtifactOut(id="01", url="u", raw_url="r", session_url="s", download_url="d", kind="file", size=1,
                         project_id="a/b", mime="text/plain", filename="f.txt", sha256="0",
                         created_at="2026-09-25T00:00:00Z").model_dump(by_alias=True)
    assert {"rawUrl", "sessionUrl", "downloadUrl", "projectId", "createdAt"} <= set(dumped)


def test_cli_version_and_credentials(paths, capsys: pytest.CaptureFixture) -> None:
    assert cli_main(["version", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["version"] and info["sourceHash"]
    credentials.save(credentials.Credentials(api_key="ehk_abcdefgh_" + "b" * 32, url="http://127.0.0.1:1"), paths)
    mode = stat.S_IMODE(os.stat(paths.credentials_file).st_mode)
    assert os.name == "nt" or mode == 0o600
    assert credentials.resolve_api_key(paths)[1] == "file"


def test_cli_client_error_mapping(paths) -> None:
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        if request.url.path == "/api/leases/zzzzzz/heartbeat":
            return httpx.Response(410, json={"error": "lease_released", "message": "gone", "reacquire": "x"})
        return httpx.Response(401, json={"error": "unauthorized", "message": "log in"})

    client = HarnessClient("http://127.0.0.1:9", "ehk_abcdefgh_" + "c" * 32, paths=paths,
                           transport=httpx.MockTransport(handler))
    with pytest.raises(Gone) as gone:
        client.post("/api/leases/zzzzzz/heartbeat")
    assert gone.value.payload["reacquire"] == "x" and gone.value.exit_code == 7
    with pytest.raises(NotAuthenticated):
        client.get("/api/auth/me")
    assert seen_headers["authorization"].startswith("Bearer ehk_")


def test_member_cannot_reach_admin_via_local_when_auth_enabled(auth_config: Config) -> None:
    auth_app = make_app(auth_config)
    database = auth_app.state.ctx.db
    user = create_user(database, "viewer1")
    key = create_key(database, user.id)
    add_grant(database, user.id, "p/q", "viewer")
    _grant_routes(auth_app)
    with TestClient(auth_app) as test_client:
        assert test_client.get("/api/_test/p/p/q", headers=bearer(key)).json()["limited"] is False
        assert test_client.get("/api/_test/admin", headers=bearer(key)).status_code == 403
