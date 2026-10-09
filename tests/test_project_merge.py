from __future__ import annotations

import json

from conftest import add_grant, create_user
from test_store_support import upload

from eks_harness.cli import main, store_cmds
from eks_harness.cli.client import HarnessClient


def _seed(client, db):
    ids = {}
    ids["web"] = upload(client, b"web shot", "home.png", "image/png", project="acme/web-app",
                        session="feature/login").json()["id"]
    ids["web2"] = upload(client, b"web log", "console.txt", "text/plain", project="acme/web-app",
                         session="development").json()["id"]
    ids["mobile"] = upload(client, b"mobile shot", "screen.png", "image/png", project="acme/mobile-app",
                           session="feature/login", tags="ios").json()["id"]
    ids["ads"] = upload(client, b"render", "ad.txt", "text/plain", project="acme/ads", session="render-1").json()["id"]
    client.patch("/api/projects/acme/ads", json={"retentionDays": 0})
    client.patch("/api/projects/acme/mobile-app", json={"retentionDays": 30})
    bob = create_user(db, "bob")
    add_grant(db, bob.id, "acme/web-app", "viewer")
    add_grant(db, bob.id, "acme/mobile-app", "editor")
    share = client.post(f"/api/artifacts/{ids['web']}/shares", json={}).json()
    return ids, bob, share


def test_dry_run_changes_nothing(client, db):
    _seed(client, db)
    plan = client.post("/api/projects/merge", json={"sources": ["acme/web-app", "acme/mobile-app"],
                                                     "into": "acme/all", "dryRun": True}).json()
    assert plan["applied"] is False and plan["created"] is True
    assert [(s["project"], s["tag"], s["artifacts"]) for s in plan["sources"]] == [
        ("acme/web-app", "web-app", 2), ("acme/mobile-app", "mobile-app", 1)]
    assert client.get("/api/projects/acme/web-app").status_code == 200
    assert client.get("/api/projects/acme/all").status_code == 404


def test_merge_moves_everything_and_tags_it(client, db):
    ids, bob, share = _seed(client, db)
    response = client.post("/api/projects/merge", json={
        "sources": ["acme/web-app", "acme/mobile-app", "acme/ads"], "into": "acme/all", "title": "Everything",
        "tags": {"acme/ads": "eksenads"}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["applied"] and result["artifacts"] == 4
    for gone in ("acme/web-app", "acme/mobile-app", "acme/ads"):
        assert client.get(f"/api/projects/{gone}").status_code == 404
    merged = client.get("/api/projects/acme/all").json()
    assert merged["title"] == "Everything" and merged["artifactCount"] == 4
    tags = {a["id"]: set(a["tags"]) for a in client.get("/api/artifacts", params={"project": "acme/all"}).json()["items"]}
    assert tags[ids["web"]] == {"web-app"} and tags[ids["mobile"]] == {"ios", "mobile-app"}
    assert tags[ids["ads"]] == {"eksenads"}
    sessions = {s["slug"]: s for s in client.get("/api/projects/acme/all/sessions").json()["items"]}
    assert set(sessions) == {"feature-login", "development", "render-1"}
    assert sessions["feature-login"]["artifactCount"] == 2
    tagged = client.get("/api/artifacts", params={"project": "acme/all", "tag": "mobile-app"}).json()["items"]
    assert [a["id"] for a in tagged] == [ids["mobile"]]
    ads = client.get(f"/api/artifacts/{ids['ads']}").json()
    assert ads["pinned"] is True
    mobile = client.get(f"/api/artifacts/{ids['mobile']}").json()
    assert mobile["retentionDays"] == 30
    with db.transaction() as conn:
        levels = {row["project_id"]: row["level"] for row in conn.execute(
            "SELECT project_id, level FROM grants WHERE user_id = ?", (bob.id,))}
    assert levels == {"acme/all": "editor"}
    assert client.get(f"/s/{share['token']}").status_code == 200
    found = client.get("/api/search", params={"q": "home"}).json()
    assert any(hit["artifact"]["id"] == ids["web"] and hit["artifact"]["projectId"] == "acme/all" for hit in found["items"])
    later = upload(client, b"new", "new.txt", "text/plain", project="acme/all", session="feature/login").json()
    assert later["sessionSlug"] == "feature-login"


def test_merge_into_existing_project_keeps_its_settings(client, db):
    _seed(client, db)
    client.post("/api/projects", json={"id": "acme/all", "title": "Kept"})
    client.patch("/api/projects/acme/all", json={"retentionDays": 30})
    result = client.post("/api/projects/merge", json={"sources": ["acme/mobile-app"], "into": "acme/all"}).json()
    assert result["created"] is False
    assert client.get("/api/projects/acme/all").json()["title"] == "Kept"


def test_merge_rejects_bad_input(client, db):
    _seed(client, db)
    for body, needle in (({"sources": [], "into": "acme/all"}, "at least one"),
                         ({"sources": ["acme/nope"], "into": "acme/all"}, "no project"),
                         ({"sources": ["acme/web-app"], "into": "acme/web-app"}, "target"),
                         ({"sources": ["acme/web-app"], "into": "bad"}, ""),
                         ({"sources": ["acme/web-app"], "into": "acme/all", "tags": {"acme/web-app": "a b"}}, "tag")):
        response = client.post("/api/projects/merge", json=body)
        assert response.status_code == 400 and needle in response.json()["message"], body


class Bridge:
    def __init__(self, client):
        import httpx

        self.httpx = httpx
        self.client = client

    def __call__(self):
        httpx = self.httpx
        test_client = self.client

        class Transport(httpx.BaseTransport):
            def handle_request(self, request):
                answer = test_client.request(request.method, str(request.url), headers=list(request.headers.multi_items()),
                                             content=request.read())
                return httpx.Response(answer.status_code, headers=list(answer.headers.multi_items()),
                                      content=answer.content, request=request)

        return Transport()


def test_cli_merge(client, db, ctx, monkeypatch, capsys):
    _seed(client, db)
    bridge = Bridge(client)
    monkeypatch.setattr(store_cmds, "_client", lambda args: HarnessClient(
        base_url="http://testserver", paths=ctx.paths, config=ctx.config, transport=bridge()))
    capsys.readouterr()
    assert main(["projects", "merge", "acme/web-app", "acme/ads", "--into", "acme/all", "--tag", "acme/ads=eksenads"]) == 0
    printed = capsys.readouterr()
    assert "dry run" in printed.out + printed.err
    assert client.get("/api/projects/acme/web-app").status_code == 200
    assert main(["projects", "merge", "acme/web-app", "acme/ads", "--into", "acme/all", "--tag", "acme/ads=eksenads",
                 "--apply", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] and {s["tag"] for s in data["sources"]} == {"web-app", "eksenads"}
    assert client.get("/api/projects/acme/web-app").status_code == 404


def test_lease_tags_reach_every_capture(client, db):
    from conftest import ensure_session

    ensure_session(db, "acme/all", "feature/x")
    lease = client.post("/api/leases/acquire", json={"kind": "browser", "project": "acme/all", "session": "feature/x",
                                                     "instance": "test-instance", "tags": ["web-app"]}).json()
    assert lease["status"] == "granted", lease
    sid = lease["sid"]
    shot = upload(client, b"png", "a.png", "image/png", sid=sid, tags="checkout").json()
    assert {"web-app", "checkout"} <= set(shot["tags"])
    again = client.post("/api/leases/acquire", json={"kind": "browser", "project": "acme/all", "session": "feature/x",
                                                     "instance": "test-instance", "tags": ["mobile-app"]}).json()
    assert again["sid"] == sid
    later = upload(client, b"png2", "b.png", "image/png", sid=sid).json()
    assert "mobile-app" in later["tags"] and "web-app" not in later["tags"]


def test_profile_and_project_tags(tmp_path):
    from eks_harness.drivers.profile import load_profile
    from eks_harness.plugins.project import load_project_config

    harness = tmp_path / "app" / ".harness"
    harness.mkdir(parents=True)
    (harness / "app.toml").write_text('[app]\nproject = "acme/all"\ntags = ["web-app"]\n', encoding="utf-8")
    assert load_profile(harness).tags == ("web-app",)
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "project.toml").write_text('[project]\nid = "acme/all"\ntags = ["repo"]\n', encoding="utf-8")
    assert load_project_config(tmp_path).tags == ("repo",)
