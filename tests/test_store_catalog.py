from __future__ import annotations

from eks_harness.daemon import events as ev
from conftest import ensure_session
from test_store_support import add_lease, png_bytes, upload


def admin_upload(env, content: bytes, filename: str, mime: str = "text/plain", **fields) -> dict:
    response = upload(env.client, content, filename, mime, headers=env.admin_headers, **fields)
    assert response.status_code == 201, response.text
    return response.json()


def test_seen_is_per_user(auth_env):
    first = admin_upload(auth_env, b"1", "one.txt", project="acme/web", session="s1")
    second = admin_upload(auth_env, b"2", "two.txt", project="acme/web", session="s1")
    alice, alice_key = auth_env.make_user("alice")
    bob, bob_key = auth_env.make_user("bob")
    auth_env.grant(alice, "acme/web", "viewer")
    auth_env.grant(bob, "acme/web", "viewer")
    client = auth_env.client
    alice_headers, bob_headers = auth_env.headers_for(alice_key), auth_env.headers_for(bob_key)
    assert client.put(f"/api/artifacts/{first['id']}/seen", headers=alice_headers).json() == {"updated": 1,
                                                                                            "seen": True}
    alice_view = client.get("/api/artifacts", params={"project": "acme/web"}, headers=alice_headers).json()
    assert {a["id"]: a["seen"] for a in alice_view["items"]} == {first["id"]: True, second["id"]: False}
    bob_view = client.get("/api/artifacts", params={"project": "acme/web"}, headers=bob_headers).json()
    assert all(not a["seen"] for a in bob_view["items"])
    unseen = client.get("/api/artifacts", params={"project": "acme/web", "unseen": "true"}, headers=alice_headers)
    assert [a["id"] for a in unseen.json()["items"]] == [second["id"]]
    seen_only = client.get("/api/artifacts", params={"project": "acme/web", "unseen": "false"}, headers=alice_headers)
    assert [a["id"] for a in seen_only.json()["items"]] == [first["id"]]
    project = client.get("/api/projects/acme/web", headers=alice_headers).json()
    assert project["unseenCount"] == 1
    assert client.get("/api/projects/acme/web", headers=bob_headers).json()["unseenCount"] == 2
    session = client.get("/api/projects/acme/web/sessions/s1", headers=alice_headers).json()
    assert session["unseenCount"] == 1

    bulk = client.post("/api/artifacts/seen", json={"ids": [first["id"], second["id"]]}, headers=bob_headers)
    assert bulk.json() == {"updated": 2, "seen": True}
    assert client.get("/api/projects/acme/web", headers=bob_headers).json()["unseenCount"] == 0
    unmark = client.post("/api/artifacts/seen", json={"ids": [first["id"]], "seen": False}, headers=bob_headers)
    assert unmark.json() == {"updated": 1, "seen": False}
    assert client.delete(f"/api/artifacts/{second['id']}/seen", headers=bob_headers).json()["updated"] == 1
    all_seen = client.post("/api/projects/acme/web/sessions/s1/seen", headers=bob_headers)
    assert all_seen.json()["updated"] == 2
    missing = client.post("/api/artifacts/seen", json={"ids": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]}, headers=bob_headers)
    assert missing.status_code == 404 and missing.json()["missing"] == ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    detail = client.get(f"/api/artifacts/{second['id']}", params={"markSeen": "true"}, headers=alice_headers)
    assert detail.json()["seen"] is True


def test_search_tags_and_captions(client):
    login = upload(client, png_bytes(), "login-page.png", "image/png", project="acme/web", session="feature/auth",
                   caption="Login form with error", tags="auth,bug").json()
    upload(client, b"x", "dashboard.txt", "text/plain", project="acme/web", session="main", caption="Dashboard",
           tags="smoke")
    upload(client, b"x", "other.txt", "text/plain", project="acme/mobile", session="main", caption="Login on phone")
    by_caption = client.get("/api/search", params={"q": "error"}).json()
    assert [h["artifact"]["id"] for h in by_caption["items"]] == [login["id"]]
    assert "[error]" in by_caption["items"][0]["snippet"]
    by_prefix = client.get("/api/search", params={"q": "logi"}).json()
    assert len(by_prefix["items"]) == 2
    scoped = client.get("/api/search", params={"q": "login", "project": "acme/mobile"}).json()
    assert [h["artifact"]["projectId"] for h in scoped["items"]] == ["acme/mobile"]
    by_session = client.get("/api/search", params={"q": "auth"}).json()
    assert by_session["items"][0]["artifact"]["id"] == login["id"]
    by_tag = client.get("/api/artifacts", params={"tag": "bug"}).json()
    assert [a["id"] for a in by_tag["items"]] == [login["id"]]
    text_filter = client.get("/api/artifacts", params={"q": "dashboard", "project": "acme/web"}).json()
    assert text_filter["total"] == 1

    patched = client.patch(f"/api/artifacts/{login['id']}", json={"caption": "Login fixed", "addTags": ["fixed"],
                                                                   "removeTags": ["bug"], "pinned": True}).json()
    assert patched["caption"] == "Login fixed"
    assert patched["tags"] == ["auth", "fixed"]
    assert patched["pinned"] is True
    assert client.get("/api/search", params={"q": "error"}).json()["items"] == []
    assert client.get("/api/search", params={"q": "fixed"}).json()["items"][0]["artifact"]["id"] == login["id"]
    replaced = client.patch(f"/api/artifacts/{login['id']}", json={"tags": ["final"]}).json()
    assert replaced["tags"] == ["final"]
    bad_tag = client.patch(f"/api/artifacts/{login['id']}", json={"tags": ["has space"]})
    assert bad_tag.status_code == 400
    tags = client.get("/api/tags", params={"project": "acme/web"}).json()
    assert [(t["tag"], t["count"], t["builtin"]) for t in tags] == [("final", 1, False), ("smoke", 1, False)]

    bulk = client.post("/api/artifacts/tags", json={"ids": [login["id"]], "add": ["x1", "x2"], "remove": ["final"]})
    assert bulk.json()["items"][0]["tags"] == ["x1", "x2"]

    renamed = client.patch("/api/projects/acme/web/sessions/feature-auth", json={"name": "feature/authentication"})
    assert renamed.json()["slug"] == "feature-authentication"
    assert client.get("/api/search", params={"q": "authentication"}).json()["items"][0]["artifact"]["id"] == login["id"]
    meta = client.patch(f"/api/artifacts/{login['id']}", json={"meta": {"url": "http://x", "extra": 1}}).json()
    assert meta["meta"] == {"url": "http://x", "extra": 1}
    cleared = client.patch(f"/api/artifacts/{login['id']}", json={"meta": {"extra": None}}).json()
    assert cleared["meta"] == {"url": "http://x"}


def test_artifact_listing_filters_and_paging(client):
    ids = [upload(client, b"x", f"f{i}.txt", "text/plain", project="acme/web", session="s").json()["id"]
           for i in range(5)]
    upload(client, png_bytes(), "p.png", "image/png", project="acme/web", session="s")
    upload(client, b"x", "root.txt", "text/plain", project="acme/web")
    first = client.get("/api/artifacts", params={"project": "acme/web", "session": "s", "kind": "log", "limit": 2})
    body = first.json()
    assert body["total"] == 5
    assert [a["id"] for a in body["items"]] == ids[::-1][:2]
    second = client.get("/api/artifacts", params={"project": "acme/web", "session": "s", "kind": "log", "limit": 2,
                                                  "cursor": body["nextCursor"]}).json()
    assert [a["id"] for a in second["items"]] == ids[::-1][2:4]
    screenshots = client.get("/api/artifacts", params={"kind": "screenshot,video"}).json()
    assert [a["filename"] for a in screenshots["items"]] == ["p.png"]
    project_level = client.get("/api/artifacts", params={"project": "acme/web", "session": "_project"}).json()
    assert [a["filename"] for a in project_level["items"]] == ["root.txt"]
    assert client.get("/api/artifacts", params={"project": "acme/web", "session": "nope"}).status_code == 404
    assert client.get("/api/artifacts", params={"project": "no/such"}).status_code == 404
    assert client.get("/api/artifacts", params={"session": "s"}).status_code == 200
    assert client.get("/api/artifacts", params={"session": "nope"}).status_code == 404
    detail = client.get(f"/api/artifacts/{ids[2]}").json()
    assert detail["previousId"] == ids[3] and detail["nextId"] == ids[1]
    pinned = client.patch(f"/api/artifacts/{ids[0]}", json={"pinned": True})
    assert pinned.status_code == 200
    assert [a["id"] for a in client.get("/api/artifacts", params={"pinned": "true"}).json()["items"]] == [ids[0]]


def test_notes_and_timeline(client, ctx):
    session = ensure_session(ctx.db, "acme/web", "feature/x")
    lease = add_lease(ctx.db, session.id, kind="browser", resource="browser-1-1")
    ctx.events.publish(ev.LEASE_ACQUIRED, resource="browser-1-1", lease_sid=lease.sid, session_id=session.id,
                       project_id="acme/web", actor="agent-1", detail={"kind": "browser"})
    ctx.events.publish(ev.LEASE_READY, resource="browser-1-1", lease_sid=lease.sid, actor="agent-1")
    ctx.events.publish(ev.DEVICE_STATUS, resource="ios-2", detail={"status": "booted"})
    note = client.post("/api/projects/acme/web/sessions/feature-x/notes", json={"body": "Starting the check",
                                                                                "sid": lease.sid})
    assert note.status_code == 201
    assert note.json()["leaseSid"] == lease.sid
    artifact = upload(client, png_bytes(), "s.png", "image/png", sid=lease.sid).json()
    by_sid = client.post(f"/api/sid/{lease.sid}/notes", json={"body": "Done"})
    assert by_sid.status_code == 201 and by_sid.json()["sessionId"] == session.id
    empty = client.post("/api/projects/acme/web/sessions/feature-x/notes", json={"body": "   "})
    assert empty.status_code == 400
    timeline = client.get("/api/projects/acme/web/sessions/feature-x/timeline").json()
    assert timeline["session"]["name"] == "feature/x"
    kinds = [(e["type"], (e.get("event") or {}).get("type")) for e in timeline["items"]]
    assert kinds == [("event", "lease.acquired"), ("event", "lease.ready"), ("note", None), ("artifact", None),
                     ("note", None)]
    assert timeline["items"][3]["artifact"]["id"] == artifact["id"]
    only_notes = client.get("/api/projects/acme/web/sessions/feature-x/timeline", params={"types": "note"}).json()
    assert [e["note"]["body"] for e in only_notes["items"]] == ["Starting the check", "Done"]
    assert client.get("/api/projects/acme/web/sessions/feature-x/timeline",
                      params={"types": "bogus"}).status_code == 400
    listed = client.get("/api/projects/acme/web/sessions/feature-x/notes").json()
    assert len(listed) == 2
    session_out = client.get("/api/projects/acme/web/sessions/feature-x").json()
    assert session_out["noteCount"] == 2
    assert [l["sid"] for l in session_out["activeLeases"]] == [lease.sid]


def test_sid_info(client, ctx):
    session = ensure_session(ctx.db, "acme/web", "main")
    lease = add_lease(ctx.db, session.id, kind="android", resource="android-1")
    info = client.get(f"/api/sid/{lease.sid}").json()
    assert info["valid"] is True
    assert info["lease"]["kind"] == "android"
    assert info["project"]["id"] == "acme/web"
    assert info["session"]["slug"] == "main"
    assert info["urls"]["session"] == "http://127.0.0.1:7171/p/acme/web/s/main"
    assert info["reacquire"] is None
    from test_store_support import end_lease
    end_lease(ctx.db, lease)
    released = client.get(f"/api/sid/{lease.sid}").json()
    assert released["valid"] is False
    assert released["reacquire"] == "eks-harness lease acquire --project acme/web --session main --kind android"
    assert client.get("/api/sid/abcdef").status_code == 404


def test_projects_crud(client):
    created = client.post("/api/projects", json={"id": "acme/docs", "title": "Docs", "retentionDays": 30})
    assert created.status_code == 201
    assert created.json()["implicit"] is False and created.json()["retentionDays"] == 30
    assert client.post("/api/projects", json={"id": "acme/docs"}).status_code == 409
    assert client.post("/api/projects", json={"owner": "Bad Owner", "name": "x"}).status_code == 400
    edited = client.patch("/api/projects/acme/docs", json={"description": "All docs", "retentionDays": None})
    assert edited.json()["description"] == "All docs" and edited.json()["retentionDays"] is None
    upload(client, b"x", "a.txt", "text/plain", project="acme/implicit")
    listed = {p["id"]: p for p in client.get("/api/projects").json()["items"]}
    assert set(listed) == {"acme/docs", "acme/implicit"}
    assert listed["acme/implicit"]["implicit"] is True
    assert listed["acme/implicit"]["artifactCount"] == 1
    made_explicit = client.patch("/api/projects/acme/implicit", json={"title": "Now explicit"}).json()
    assert made_explicit["implicit"] is False
    assert client.get("/api/projects/acme/none").status_code == 404
