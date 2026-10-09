from __future__ import annotations

import sqlite3

from conftest import AuthEnv, add_grant
from test_store_support import local_path, upload

from eks_harness.db import migrations


def _upload(client, project: str, session: str, name: str, headers: dict | None = None) -> dict:
    response = upload(client, b"log line", name, "text/plain", headers=headers, project=project, session=session)
    assert response.status_code == 201, response.text
    return response.json()


def test_one_session_spans_projects(client) -> None:
    web = _upload(client, "acme/web", "feature/login", "web.log")
    mobile = _upload(client, "acme/mobile", "feature/login", "mobile.log")
    listed = client.get("/api/sessions").json()["items"]
    assert len(listed) == 1
    session = listed[0]
    assert session["slug"] == "feature-login" and session["artifactCount"] == 2
    assert sorted(p["project"]["id"] for p in session["projects"]) == ["acme/mobile", "acme/web"]
    assert session["sharedUrl"].endswith("/sessions/feature-login")
    for project in session["projects"]:
        assert project["artifactCount"] == 1
        assert project["url"].endswith(f"/p/{project['project']['id']}/s/feature-login")
    scoped = client.get("/api/projects/acme/web/sessions/feature-login").json()
    assert scoped["projectId"] == "acme/web" and scoped["artifactCount"] == 1
    both = client.get("/api/artifacts", params={"session": "feature-login"}).json()
    assert {a["id"] for a in both["items"]} == {web["id"], mobile["id"]}
    only_web = client.get("/api/artifacts", params={"project": "acme/web", "session": "feature-login"}).json()
    assert [a["id"] for a in only_web["items"]] == [web["id"]]
    timeline = client.get("/api/sessions/feature-login/timeline", params={"types": "artifact"}).json()
    assert len(timeline["items"]) == 2
    assert len(client.get("/api/projects/acme/mobile/sessions/feature-login/timeline",
                          params={"types": "artifact"}).json()["items"]) == 1


def test_project_scoped_delete_keeps_the_other_project(client) -> None:
    _upload(client, "acme/web", "shared", "web.log")
    mobile = _upload(client, "acme/mobile", "shared", "mobile.log")
    summary = client.delete("/api/projects/acme/web/sessions/shared").json()
    assert summary["artifacts"] == 1 and summary["sessions"] == 0
    session = client.get("/api/sessions/shared").json()
    assert [p["project"]["id"] for p in session["projects"]] == ["acme/mobile"]
    assert client.get("/api/projects/acme/web/sessions/shared").status_code == 404
    assert client.get(local_path(mobile["rawUrl"])).status_code == 200
    summary = client.delete("/api/projects/acme/mobile/sessions/shared").json()
    assert summary["sessions"] == 1
    assert client.get("/api/sessions/shared").status_code == 404


def test_project_delete_leaves_shared_sessions(client) -> None:
    _upload(client, "acme/web", "shared", "web.log")
    _upload(client, "acme/web", "web-only", "web.log")
    _upload(client, "acme/mobile", "shared", "mobile.log")
    assert client.delete("/api/projects/acme/web").json()["sessions"] == 1
    assert [s["slug"] for s in client.get("/api/sessions").json()["items"]] == ["shared"]


def test_rename_is_global(client) -> None:
    _upload(client, "acme/web", "old", "web.log")
    _upload(client, "acme/mobile", "old", "mobile.log")
    renamed = client.patch("/api/projects/acme/web/sessions/old", json={"name": "new name"}).json()
    assert renamed["slug"] == "new-name"
    assert client.get("/api/projects/acme/mobile/sessions/new-name").status_code == 200


def test_viewers_only_see_their_projects(auth_env: AuthEnv) -> None:
    client, admin = auth_env.client, auth_env.admin_headers
    _upload(client, "acme/web", "feature/x", "web.log", admin)
    _upload(client, "acme/mobile", "feature/x", "mobile.log", admin)
    user, key = auth_env.make_user("viewer")
    add_grant(auth_env.db, user.id, "acme/web", "viewer")
    headers = {"Authorization": f"Bearer {key}"}
    session = client.get("/api/sessions/feature-x", headers=headers).json()
    assert [p["project"]["id"] for p in session["projects"]] == ["acme/web"]
    assert session["artifactCount"] == 1
    items = client.get("/api/artifacts", params={"session": "feature-x"}, headers=headers).json()["items"]
    assert [a["projectId"] for a in items] == ["acme/web"]
    assert client.patch("/api/sessions/feature-x", json={"name": "y"}, headers=headers).status_code == 403
    outsider, other_key = auth_env.make_user("outsider")
    assert client.get("/api/sessions/feature-x", headers={"Authorization": f"Bearer {other_key}"}).status_code == 404


def _legacy_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for migration in migrations.discover():
        if migration.version >= 3:
            break
        conn.executescript(migration.sql)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "applied_at REAL NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES (1, 'schema', 0), (2, 'section11', 0)")
    return conn


def test_migration_merges_same_name_sessions() -> None:
    conn = _legacy_db()
    for project in ("acme/web", "acme/mobile"):
        conn.execute("INSERT INTO projects (id, owner, name, created_at, updated_at) VALUES (?, 'acme', ?, 1, 1)",
                     (project, project.split("/")[1]))
    conn.execute("INSERT INTO sessions (id, project_id, name, slug, created_at, last_active_at) VALUES "
                 "(1, 'acme/web', 'feature/x', 'feature-x', 10, 20), "
                 "(2, 'acme/mobile', 'feature/x', 'feature-x', 5, 30), "
                 "(3, 'acme/mobile', 'solo', 'solo', 1, 1)")
    conn.execute("INSERT INTO artifacts (id, project_id, session_id, kind, filename, rel_path, created_at) VALUES "
                 "('A1', 'acme/web', 1, 'log', 'a.log', 'acme/web/feature-x/A1/a.log', 1), "
                 "('A2', 'acme/mobile', 2, 'log', 'b.log', 'acme/mobile/feature-x/A2/b.log', 1)")
    conn.execute("INSERT INTO notes (session_id, author, body, created_at) VALUES (2, 'me', 'hi', 1)")
    conn.execute("INSERT INTO leases (id, sid, kind, session_id, owner_instance, state, queued_at) VALUES "
                 "('L1', 'abc123', 'ios', 2, 'x', 'released', 1)")
    assert migrations.migrate(conn)[:2] == [3, 4]
    sessions = conn.execute("SELECT id, slug, created_at, last_active_at FROM sessions ORDER BY id").fetchall()
    assert [tuple(r) for r in sessions] == [(1, "feature-x", 5, 30), (3, "solo", 1, 1)]
    assert {r[0] for r in conn.execute("SELECT DISTINCT session_id FROM artifacts")} == {1}
    assert conn.execute("SELECT session_id FROM notes").fetchone()[0] == 1
    assert tuple(conn.execute("SELECT session_id, project_id FROM leases").fetchone()) == (1, "acme/mobile")
    links = conn.execute("SELECT session_id, project_id FROM session_projects ORDER BY 1, 2").fetchall()
    assert [tuple(r) for r in links] == [(1, "acme/mobile"), (1, "acme/web"), (3, "acme/mobile")]
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
