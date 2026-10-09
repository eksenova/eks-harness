from __future__ import annotations

import time

from conftest import ensure_session
from test_store_support import upload

DAY = 86400


def _age(db, artifact_id: str, days: float) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE artifacts SET created_at = ? WHERE id = ?", (time.time() - days * DAY, artifact_id))


def _idle(db, slug: str, days: float) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET last_active_at = ? WHERE slug = ?", (time.time() - days * DAY, slug))


def _seed(client, db) -> dict[str, str]:
    ids = {}
    ids["old_shot"] = upload(client, b"a" * 100, "old.png", "image/png", project="acme/all", session="feature/old",
                             tags="web-app").json()["id"]
    ids["old_video"] = upload(client, b"b" * 5000, "old.mp4", "video/mp4", project="acme/all", session="feature/old",
                              tags="web-app,demo").json()["id"]
    ids["old_pinned"] = upload(client, b"c" * 10, "keep.png", "image/png", project="acme/all", session="feature/old",
                               tags="web-app").json()["id"]
    ids["old_shared"] = upload(client, b"d" * 10, "shared.png", "image/png", project="acme/all",
                               session="feature/shared", tags="mobile-app").json()["id"]
    ids["new_shot"] = upload(client, b"e" * 10, "new.png", "image/png", project="acme/all", session="feature/new",
                             tags="web-app").json()["id"]
    ids["other"] = upload(client, b"f" * 10, "other.png", "image/png", project="acme/other",
                          session="feature/old").json()["id"]
    for key in ("old_shot", "old_video", "old_pinned", "old_shared", "other"):
        _age(db, ids[key], 40)
    client.patch(f"/api/artifacts/{ids['old_pinned']}", json={"pinned": True})
    client.post(f"/api/artifacts/{ids['old_shared']}/shares", json={})
    return ids


def _preview(client, headers=None, **filter_):
    response = client.post("/api/cleanup/preview", json={"filter": filter_, "sample": 10}, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_counts_groups_and_protects_pinned_and_shared(client, db):
    ids = _seed(client, db)
    data = _preview(client, olderThanDays=30)
    assert data["count"] == 3 and data["bytes"] == 5110
    assert data["matched"] == {"count": 5, "bytes": 5130}
    assert data["skipped"] == {"pinned": 1, "shared": 1, "live": 0}
    assert {p["project"]: p["count"] for p in data["projects"]} == {"acme/all": 2, "acme/other": 1}
    assert {k["kind"] for k in data["kinds"]} == {"screenshot", "video"}
    assert {t["tag"]: t["count"] for t in data["tags"]} == {"web-app": 2, "demo": 1}
    assert data["sample"][0]["id"] in {ids["old_shot"], ids["old_video"], ids["other"]}
    assert all("/p/" in s["url"] for s in data["sample"])
    wider = _preview(client, olderThanDays=30, includePinned=True, includeShared=True)
    assert wider["count"] == 5 and wider["skipped"] == {"pinned": 0, "shared": 0, "live": 0}


def test_filters_combine(client, db):
    ids = _seed(client, db)
    assert _preview(client, olderThanDays=30, projects=["acme/all"], kinds=["video"])["count"] == 1
    assert _preview(client, olderThanDays=30, tagsNone=["demo"])["count"] == 2
    assert _preview(client, tagsAny=["demo", "mobile-app"], includeShared=True)["count"] == 2
    assert _preview(client, tagsAll=["web-app", "demo"])["count"] == 1
    assert _preview(client, minSize=1000)["count"] == 1
    assert _preview(client, sessionPattern="feature-n*")["sample"][0]["id"] == ids["new_shot"]
    assert _preview(client, sessionPattern="feature/*", excludeSessions=["feature/old"])["count"] == 1
    assert _preview(client, q="other")["count"] == 1
    assert _preview(client, seen="never", olderThanDays=30)["count"] == 3
    client.post("/api/artifacts/seen", json={"ids": [ids["old_shot"]]})
    assert _preview(client, seen="never", olderThanDays=30)["count"] == 2
    _idle(db, "feature-old", 20)
    assert _preview(client, sessionIdleDays=10)["count"] == 3
    order = _preview(client, olderThanDays=30)
    assert order["oldest"] <= order["newest"]
    bad = client.post("/api/cleanup/preview", json={"filter": {"sessions": ["nope"]}})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_filter"


def test_apply_deletes_matching_and_empty_sessions(client, db, ctx):
    ids = _seed(client, db)
    ensure_session(db, "acme/all", "feature/noted")
    noted = upload(client, b"n", "n.png", "image/png", project="acme/all", session="feature/noted").json()["id"]
    _age(db, noted, 40)
    note = client.post("/api/projects/acme/all/sessions/feature-noted/notes", json={"body": "keep me"})
    assert note.status_code == 201, note.text
    preview = _preview(client, olderThanDays=30, projects=["acme/all"])
    stale = client.post("/api/cleanup/apply", json={"filter": {"olderThanDays": 30, "projects": ["acme/all"]},
                                                     "expectCount": preview["count"] + 1})
    assert stale.status_code == 409 and stale.json()["error"] == "cleanup_changed"
    response = client.post("/api/cleanup/apply", json={"filter": {"olderThanDays": 30, "projects": ["acme/all"]},
                                                        "expectCount": preview["count"],
                                                        "removeEmptySessions": True})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["artifacts"] == 3 and result["bytes"] == 5101
    for gone in ("old_shot", "old_video"):
        assert client.get(f"/api/artifacts/{ids[gone]}").status_code == 404
    for kept in ("old_pinned", "old_shared", "new_shot", "other"):
        assert client.get(f"/api/artifacts/{ids[kept]}").status_code == 200
    assert client.get(f"/api/artifacts/{noted}").status_code == 404
    slugs = {s["slug"] for s in client.get("/api/sessions").json()["items"]}
    assert "feature-old" in slugs and "feature-noted" in slugs
    assert not any(ctx.paths.store_dir.rglob("old.mp4"))


def test_apply_removes_sessions_left_empty(client, db):
    only = upload(client, b"x", "x.png", "image/png", project="acme/all", session="feature/gone").json()["id"]
    _age(db, only, 40)
    preview = _preview(client, sessions=["feature/gone"])
    assert preview["emptySessions"] == 1
    result = client.post("/api/cleanup/apply", json={"filter": {"sessions": ["feature/gone"]}, "expectCount": 1,
                                                      "removeEmptySessions": True}).json()
    assert result["artifacts"] == 1 and result["sessions"] == 1
    assert "feature-gone" not in {s["slug"] for s in client.get("/api/sessions").json()["items"]}


def test_apply_needs_a_condition(client, db):
    _seed(client, db)
    response = client.post("/api/cleanup/apply", json={"filter": {}, "expectCount": 4})
    assert response.status_code == 400 and "at least one condition" in response.json()["message"]
    assert _preview(client)["empty"] is True


def test_cleanup_only_reaches_projects_the_user_edits(auth_env):
    client, admin = auth_env.client, auth_env.admin_headers
    for project in ("acme/a", "acme/b", "acme/c"):
        response = upload(client, b"x", f"{project[-1]}.png", "image/png", headers=admin, project=project,
                          session="s1")
        assert response.status_code == 201, response.text
    user, key = auth_env.make_user("bob")
    auth_env.grant(user, "acme/a", "editor")
    auth_env.grant(user, "acme/b", "viewer")
    mine = auth_env.headers_for(key)
    data = _preview(client, headers=mine, q="png")
    assert {p["project"] for p in data["projects"]} == {"acme/a"}
    result = client.post("/api/cleanup/apply", json={"filter": {"q": "png"}, "expectCount": 1}, headers=mine).json()
    assert result["artifacts"] == 1
    assert _preview(client, headers=admin, q="png")["count"] == 2
