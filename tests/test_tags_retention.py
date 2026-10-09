from __future__ import annotations

import sqlite3
import time

import pytest

from eks_harness.db import migrations
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import tags as tags_repo
from eks_harness.store import deletion, retention
from test_store_support import add_lease, png_bytes, upload
from conftest import ensure_session


def _age(ctx, artifact_id: str, days: float) -> None:
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE artifacts SET created_at = ? WHERE id = ?", (time.time() - days * 86400, artifact_id))


@pytest.mark.parametrize(("command", "expected"), [
    ("chrome", "chrome"),
    ("chrome-beta", "chrome"),
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chrome"),
    ("chromium", "chromium"),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "edge"),
    ("brave", "brave"),
    ("/opt/custom/browser", None),
    (None, None),
])
def test_browser_tag_from_the_configured_command(command, expected):
    assert tags_repo.browser_tag(command) == expected


def test_builtin_tags_per_lease_kind():
    assert tags_repo.builtin_tags_for("browser", "chrome") == ["web", "chrome"]
    assert tags_repo.builtin_tags_for("browser", "/opt/x") == ["web"]
    assert tags_repo.builtin_tags_for("ios") == ["mobile", "ios"]
    assert tags_repo.builtin_tags_for("android") == ["mobile", "android"]
    assert tags_repo.builtin_tags_for(None) == []


def test_captures_get_builtin_tags_from_their_lease(client, ctx):
    session = ensure_session(ctx.db, "acme/web-app", "feature/x")
    browser = add_lease(ctx.db, session.id, kind="browser", resource="browser-1-1")
    ios = add_lease(ctx.db, session.id, kind="ios", resource="ios-1")
    android = add_lease(ctx.db, session.id, kind="android", resource="android-1")
    web_shot = upload(client, png_bytes(), "w.png", "image/png", sid=browser.sid, tags="login").json()
    ios_shot = upload(client, png_bytes(), "i.png", "image/png", sid=ios.sid).json()
    android_shot = upload(client, png_bytes(), "a.png", "image/png", sid=android.sid).json()
    plain = upload(client, png_bytes(), "p.png", "image/png", project="acme/web-app").json()
    assert web_shot["tags"] == ["chrome", "login", "web"]
    assert ios_shot["tags"] == ["ios", "mobile"]
    assert android_shot["tags"] == ["android", "mobile"]
    assert plain["tags"] == []
    removed = client.patch(f"/api/artifacts/{web_shot['id']}", json={"removeTags": ["chrome"]}).json()
    assert removed["tags"] == ["login", "web"]
    listed = client.get("/api/artifacts", params={"tag": "mobile,ios"}).json()["items"]
    assert [a["id"] for a in listed] == [ios_shot["id"]]


def test_tag_catalog_lists_builtins_and_shared_colors(client, ctx):
    upload(client, png_bytes(), "a.png", "image/png", project="acme/web", tags="smoke")
    upload(client, png_bytes(), "b.png", "image/png", project="acme/mobile", tags="smoke")
    catalog = {t["tag"]: t for t in client.get("/api/tags/catalog").json()["items"]}
    assert {"web", "chrome", "ios", "android", "mobile"} <= set(catalog)
    assert catalog["ios"]["builtin"] and catalog["ios"]["label"] == "iOS" and catalog["ios"]["color"]
    assert catalog["smoke"] == {"tag": "smoke", "label": "smoke", "color": None, "defaultColor": None,
                                "builtin": False, "description": "", "count": 2}

    colored = client.put("/api/tags/smoke/color", json={"color": "#A0B"}).json()
    assert colored["color"] == "#aa00bb"
    for project in ("acme/web", "acme/mobile"):
        tags = client.get("/api/tags", params={"project": project}).json()
        assert tags == [{"tag": "smoke", "count": 1, "color": "#aa00bb", "builtin": False, "label": None}]

    overridden = client.put("/api/tags/ios/color", json={"color": "#112233"}).json()
    assert overridden["color"] == "#112233" and overridden["defaultColor"] != "#112233"
    reset = client.put("/api/tags/ios/color", json={"color": None}).json()
    assert reset["color"] == reset["defaultColor"]

    bad = client.put("/api/tags/smoke/color", json={"color": "red"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_tag_color"


def test_tag_colors_need_editor_access(auth_env):
    viewer, viewer_key = auth_env.make_user("viewer")
    auth_env.grant(viewer, "acme/web", "viewer")
    editor, editor_key = auth_env.make_user("editor")
    auth_env.grant(editor, "acme/web", "editor")
    denied = auth_env.client.put("/api/tags/smoke/color", json={"color": "#123456"},
                                 headers=auth_env.headers_for(viewer_key))
    assert denied.status_code == 403
    allowed = auth_env.client.put("/api/tags/smoke/color", json={"color": "#123456"},
                                  headers=auth_env.headers_for(editor_key))
    assert allowed.status_code == 200


def test_project_retention_inherits_overrides_or_keeps_forever(client, ctx):
    inherit = client.post("/api/projects", json={"id": "acme/inherit"}).json()
    assert inherit["retentionDays"] is None and inherit["retentionSource"] == "global"
    assert inherit["effectiveRetentionDays"] is None
    ctx.config.set("retention.defaultDays", 30)
    inherit = client.get("/api/projects/acme/inherit").json()
    assert inherit["defaultRetentionDays"] == 30 and inherit["effectiveRetentionDays"] == 30
    forever = client.patch("/api/projects/acme/inherit", json={"retentionDays": 0}).json()
    assert forever["retentionSource"] == "forever" and forever["effectiveRetentionDays"] is None
    own = client.patch("/api/projects/acme/inherit", json={"retentionDays": 7}).json()
    assert own["retentionSource"] == "project" and own["effectiveRetentionDays"] == 7
    back = client.patch("/api/projects/acme/inherit", json={"retentionDays": None}).json()
    assert back["retentionSource"] == "global" and back["effectiveRetentionDays"] == 30
    assert client.patch("/api/projects/acme/inherit", json={"retentionDays": -1}).status_code == 422


def test_artifact_retention_overrides_project_and_global(client, ctx):
    client.post("/api/projects", json={"id": "acme/forever", "retentionDays": 0})
    client.post("/api/projects", json={"id": "acme/short", "retentionDays": 3})
    own = upload(client, b"own", "own.txt", "text/plain", project="acme/forever").json()
    kept = upload(client, b"kept", "kept.txt", "text/plain", project="acme/forever").json()
    longer = upload(client, b"longer", "longer.txt", "text/plain", project="acme/short").json()
    project_rule = upload(client, b"proj", "proj.txt", "text/plain", project="acme/short").json()
    pinned = upload(client, b"pin", "pin.txt", "text/plain", project="acme/short").json()
    global_rule = upload(client, b"glob", "glob.txt", "text/plain", project="acme/inherit").json()

    detail = client.patch(f"/api/artifacts/{own['id']}", json={"retentionDays": 5}).json()
    assert detail["retentionDays"] == 5 and detail["retentionSource"] == "artifact"
    assert detail["effectiveRetentionDays"] == 5 and detail["expiresAt"]
    client.patch(f"/api/artifacts/{longer['id']}", json={"retentionDays": 60})
    client.patch(f"/api/artifacts/{pinned['id']}", json={"pinned": True, "retentionDays": 1})
    shown = client.get(f"/api/artifacts/{kept['id']}").json()
    assert shown["retentionSource"] == "project" and shown["effectiveRetentionDays"] is None
    assert shown["expiresAt"] is None
    assert client.get(f"/api/artifacts/{pinned['id']}").json()["retentionSource"] == "pinned"

    for item in (own, kept, longer, project_rule, pinned, global_rule):
        _age(ctx, item["id"], 10)
    report = retention.run_retention(ctx.db, ctx.config)
    assert report.projects == {"acme/forever": 1, "acme/short": 1}
    remaining = {a["id"] for a in client.get("/api/artifacts").json()["items"]}
    assert remaining == {kept["id"], longer["id"], pinned["id"], global_rule["id"]}

    ctx.config.set("retention.defaultDays", 5)
    assert retention.run_retention(ctx.db, ctx.config).projects == {"acme/inherit": 1}

    reset = client.patch(f"/api/artifacts/{longer['id']}", json={"retentionDays": None}).json()
    assert reset["retentionDays"] is None and reset["retentionSource"] == "project"
    assert retention.run_retention(ctx.db, ctx.config).projects == {"acme/short": 1}


def test_bulk_retention_sets_and_clears_overrides(client, ctx):
    first = upload(client, b"1", "1.txt", "text/plain", project="acme/web").json()
    second = upload(client, b"2", "2.txt", "text/plain", project="acme/web").json()
    result = client.post("/api/artifacts/retention", json={"ids": [first["id"], second["id"]], "retentionDays": 14})
    assert [a["retentionDays"] for a in result.json()["items"]] == [14, 14]
    cleared = client.post("/api/artifacts/retention", json={"ids": [first["id"]], "retentionDays": None}).json()
    assert cleared["items"][0]["retentionDays"] is None
    assert client.post("/api/artifacts/retention", json={"ids": [first["id"]], "retentionDays": 0}).status_code == 422


def test_retention_skips_an_artifact_whose_override_was_lengthened_meanwhile(client, ctx):
    item = upload(client, b"x", "x.txt", "text/plain", project="acme/web").json()
    client.patch(f"/api/artifacts/{item['id']}", json={"retentionDays": 2})
    _age(ctx, item["id"], 5)
    stamp = time.time()
    candidates = artifacts_repo.retention_candidates(ctx.db.conn(), "acme/web", None, stamp)
    assert [a.id for a in candidates] == [item["id"]]
    client.patch(f"/api/artifacts/{item['id']}", json={"retentionDays": 30})
    plan = deletion.delete_artifacts(ctx.db, ctx.paths, [item["id"]],
                                     still_expired=artifacts_repo.expired_clause(None, stamp))
    assert plan.artifact_ids == []


def test_migration_backfills_builtin_tags_and_allows_forever_projects(monkeypatch):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    every = migrations.discover()
    monkeypatch.setattr(migrations, "discover", lambda: [m for m in every if m.version < 4])
    assert migrations.migrate(conn) == [1, 2, 3]
    monkeypatch.setattr(migrations, "discover", lambda: every)
    conn.execute("INSERT INTO projects (id, owner, name, created_at, updated_at) VALUES ('a/b', 'a', 'b', 1, 1)")
    conn.execute("INSERT INTO sessions (id, name, slug, created_at, last_active_at) VALUES (1, 's', 's', 1, 1)")
    for sid, kind in (("bbbbbb", "browser"), ("iiiiii", "ios"), ("dddddd", "android")):
        conn.execute("INSERT INTO leases (id, sid, kind, session_id, owner_instance, state, queued_at, project_id) "
                     "VALUES (?, ?, ?, 1, 'x', 'released', 1, 'a/b')", (sid.upper(), sid, kind))
        conn.execute("INSERT INTO artifacts (id, project_id, session_id, lease_sid, kind, filename, rel_path, "
                     "created_at) VALUES (?, 'a/b', 1, ?, 'screenshot', 'x.png', 'x', 1)", (kind.upper(), sid))
    assert migrations.migrate(conn)[:1] == [4]
    tags = {}
    for row in conn.execute("SELECT artifact_id, tag FROM artifact_tags ORDER BY tag"):
        tags.setdefault(row[0], []).append(row[1])
    assert tags == {"BROWSER": ["web"], "IOS": ["ios", "mobile"], "ANDROID": ["android", "mobile"]}
    conn.execute("UPDATE projects SET retention_days = 0 WHERE id = 'a/b'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE artifacts SET retention_days = 0 WHERE id = 'IOS'")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
