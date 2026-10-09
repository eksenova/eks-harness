from __future__ import annotations

from eks_harness.auth.core import COOKIE_NAME, CSRF_HEADER, create_web_session
from test_store_support import local_path, png_bytes, upload


def seed(env) -> dict[str, dict]:
    made = {}
    for key, project, session in (("web-a", "acme/web", "a"), ("web-b", "acme/web", "b"),
                                  ("web-root", "acme/web", None), ("other", "acme/other", "x")):
        response = upload(env.client, png_bytes(), f"{key}.png", "image/png", headers=env.admin_headers,
                          project=project, session=session, caption=f"caption {key}")
        assert response.status_code == 201, response.text
        made[key] = response.json()
    return made


def test_member_without_grants_sees_nothing(auth_env):
    made = seed(auth_env)
    _, key = auth_env.make_user("nobody")
    headers = auth_env.headers_for(key)
    client = auth_env.client
    assert client.get("/api/projects", headers=headers).json()["items"] == []
    assert client.get("/api/artifacts", headers=headers).json()["items"] == []
    assert client.get("/api/search", params={"q": "caption"}, headers=headers).json()["items"] == []
    assert client.get(f"/api/artifacts/{made['web-a']['id']}", headers=headers).status_code == 404
    assert client.get(local_path(made["web-a"]["rawUrl"]), headers=headers).status_code == 404
    assert client.get("/api/projects/acme/web", headers=headers).status_code == 404
    denied = upload(client, b"x", "a.txt", "text/plain", headers=headers, project="acme/web")
    assert denied.status_code == 404
    new_project = upload(client, b"x", "a.txt", "text/plain", headers=headers, project="mine/new")
    assert new_project.status_code == 404
    assert client.get("/api/tags", headers=headers).json() == []


def test_unauthenticated_requests(auth_env):
    made = seed(auth_env)
    client = auth_env.client
    assert client.get("/api/artifacts").status_code == 401
    assert client.get(local_path(made["web-a"]["rawUrl"])).status_code == 401
    assert client.get(f"/thumb/{made['web-a']['id']}.jpg").status_code == 401
    assert upload(client, b"x", "a.txt", "text/plain", project="acme/web").status_code == 401
    wrong = client.get("/api/artifacts", headers={"Authorization": "Bearer ehk_AAAAAAAA_" + "B" * 32})
    assert wrong.status_code == 401


def test_viewer_reads_but_cannot_write(auth_env):
    made = seed(auth_env)
    user, key = auth_env.make_user("viewer")
    auth_env.grant(user, "acme/web", "viewer")
    headers = auth_env.headers_for(key)
    client = auth_env.client
    visible = {a["id"] for a in client.get("/api/artifacts", headers=headers).json()["items"]}
    assert visible == {made["web-a"]["id"], made["web-b"]["id"], made["web-root"]["id"]}
    assert client.get(local_path(made["web-a"]["rawUrl"]), headers=headers).status_code == 200
    assert [p["id"] for p in client.get("/api/projects", headers=headers).json()["items"]] == ["acme/web"]
    assert client.get("/api/projects/acme/web", headers=headers).json()["access"] == "viewer"
    assert upload(client, b"x", "a.txt", "text/plain", headers=headers, project="acme/web").status_code == 403
    artifact_id = made["web-a"]["id"]
    assert client.patch(f"/api/artifacts/{artifact_id}", json={"caption": "x"}, headers=headers).status_code == 403
    assert client.delete(f"/api/artifacts/{artifact_id}", headers=headers).status_code == 403
    assert client.put(f"/api/artifacts/{artifact_id}/seen", headers=headers).status_code == 200
    assert client.post("/api/projects/acme/web/sessions/a/notes", json={"body": "x"},
                       headers=headers).status_code == 403
    assert client.patch("/api/projects/acme/web", json={"title": "x"}, headers=headers).status_code == 403
    assert client.delete("/api/projects/acme/web", headers=headers).status_code == 403
    assert client.post("/api/projects", json={"id": "acme/new"}, headers=headers).status_code == 403
    assert client.get(f"/api/artifacts/{made['other']['id']}", headers=headers).status_code == 404


def test_session_grant_limits_to_that_session(auth_env):
    made = seed(auth_env)
    user, key = auth_env.make_user("tester")
    auth_env.grant(user, "acme/web", "editor", session_name="a")
    headers = auth_env.headers_for(key)
    client = auth_env.client
    visible = {a["id"] for a in client.get("/api/artifacts", headers=headers).json()["items"]}
    assert visible == {made["web-a"]["id"]}
    projects = client.get("/api/projects", headers=headers).json()["items"]
    assert [(p["id"], p["sessionCount"], p["artifactCount"]) for p in projects] == [("acme/web", 1, 1)]
    sessions = client.get("/api/projects/acme/web/sessions", headers=headers).json()["items"]
    assert [s["slug"] for s in sessions] == ["a"]
    assert client.get("/api/projects/acme/web/sessions/b", headers=headers).status_code == 404
    assert client.get(local_path(made["web-b"]["rawUrl"]), headers=headers).status_code == 404
    assert client.get(local_path(made["web-root"]["rawUrl"]), headers=headers).status_code == 404
    ok = upload(client, b"x", "mine.txt", "text/plain", headers=headers, project="acme/web", session="a")
    assert ok.status_code == 201
    new_session = upload(client, b"x", "a.txt", "text/plain", headers=headers, project="acme/web", session="c")
    assert new_session.status_code == 404
    project_level = upload(client, b"x", "a.txt", "text/plain", headers=headers, project="acme/web")
    assert project_level.status_code == 404
    listing = client.get("/api/artifacts", params={"project": "acme/web", "session": "b"}, headers=headers)
    assert listing.status_code == 404
    hits = client.get("/api/search", params={"q": "caption"}, headers=headers).json()["items"]
    assert [h["artifact"]["id"] for h in hits] == [made["web-a"]["id"]]


def test_project_editor_can_create_sessions_by_upload(auth_env):
    seed(auth_env)
    user, key = auth_env.make_user("editor")
    auth_env.grant(user, "acme/web", "editor")
    headers = auth_env.headers_for(key)
    response = upload(auth_env.client, b"x", "a.txt", "text/plain", headers=headers, project="acme/web",
                      session="feature/new")
    assert response.status_code == 201
    assert response.json()["createdBy"] == "editor"
    assert response.json()["source"] == "cli"
    deleted = auth_env.client.delete(f"/api/artifacts/{response.json()['id']}", headers=headers)
    assert deleted.status_code == 200


def test_cookie_requests_need_csrf_for_writes(auth_env):
    token, record = create_web_session(auth_env.db, auth_env.admin.id, 1)
    client = auth_env.client
    client.cookies.set(COOKIE_NAME, token)
    try:
        without = upload(client, b"x", "a.txt", "text/plain", project="acme/web")
        assert without.status_code == 403 and without.json()["error"] == "csrf_failed"
        with_csrf = upload(client, b"x", "a.txt", "text/plain", headers={CSRF_HEADER: record.csrf},
                           project="acme/web")
        assert with_csrf.status_code == 201
        assert with_csrf.json()["source"] == "ui"
        assert client.get(local_path(with_csrf.json()["rawUrl"])).status_code == 200
    finally:
        client.cookies.clear()
