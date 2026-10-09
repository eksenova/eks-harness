from __future__ import annotations

import os
import time

import pytest

from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import notes as notes_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos import shares as shares_repo
from eks_harness.store import retention
from conftest import ensure_session
from test_store_support import add_lease, png_bytes, upload


def artifact_dir(ctx, artifact_id):
    artifact = artifacts_repo.get(ctx.db.conn(), artifact_id)
    return ctx.paths.store_dir / os.path.dirname(artifact.rel_path)


def test_delete_artifact(client, ctx):
    body = upload(client, png_bytes(), "a.png", "image/png", project="acme/web", session="s",
                  caption="unique-caption").json()
    folder = artifact_dir(ctx, body["id"])
    assert folder.is_dir()
    client.post(f"/api/artifacts/{body['id']}/shares", json={})
    preview = client.delete(f"/api/artifacts/{body['id']}", params={"dryRun": "true"}).json()
    assert preview == {"deleted": False, "artifacts": 1, "sessions": 0, "notes": 0, "shares": 1,
                       "bytes": body["size"]}
    assert folder.is_dir()
    result = client.delete(f"/api/artifacts/{body['id']}").json()
    assert result["deleted"] is True and result["artifacts"] == 1
    assert not folder.exists()
    assert not folder.parent.exists()
    assert client.get(f"/api/artifacts/{body['id']}").status_code == 404
    assert client.get("/api/search", params={"q": "unique"}).json()["items"] == []
    assert client.delete(f"/api/artifacts/{body['id']}").status_code == 404


def test_bulk_delete(client, ctx):
    ids = [upload(client, b"x", f"{i}.txt", "text/plain", project="acme/web").json()["id"] for i in range(3)]
    preview = client.post("/api/artifacts/delete", json={"ids": ids[:2]}, params={"dryRun": "true"}).json()
    assert preview["artifacts"] == 2 and preview["deleted"] is False
    result = client.post("/api/artifacts/delete", json={"ids": ids[:2]}).json()
    assert result["artifacts"] == 2
    remaining = client.get("/api/artifacts").json()["items"]
    assert [a["id"] for a in remaining] == [ids[2]]


def test_delete_session(client, ctx):
    session = ensure_session(ctx.db, "acme/web", "feature/x")
    first = upload(client, b"one", "a.txt", "text/plain", project="acme/web", session="feature/x").json()
    upload(client, b"two", "b.txt", "text/plain", project="acme/web", session="feature/x")
    keep = upload(client, b"keep", "c.txt", "text/plain", project="acme/web", session="other").json()
    client.post("/api/projects/acme/web/sessions/feature-x/notes", json={"body": "hello"})
    client.post(f"/api/artifacts/{first['id']}/shares", json={"expires": "1d"})
    lease = add_lease(ctx.db, session.id)
    preview = client.delete("/api/projects/acme/web/sessions/feature-x", params={"dryRun": "true"}).json()
    assert preview == {"deleted": False, "artifacts": 2, "sessions": 1, "notes": 1, "shares": 1, "bytes": 6}
    blocked = client.delete("/api/projects/acme/web/sessions/feature-x")
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "session_in_use" and blocked.json()["sids"] == [lease.sid]
    forced = client.delete("/api/projects/acme/web/sessions/feature-x", params={"force": "true"}).json()
    assert forced["deleted"] is True and forced["artifacts"] == 2 and forced["notes"] == 1
    conn = ctx.db.conn()
    assert sessions_repo.get(conn, session.id) is None
    assert notes_repo.count_for_session(conn, session.id) == 0
    assert leases_repo.get(conn, lease.id).session_id is None
    assert not (ctx.paths.store_dir / "acme" / "web" / "feature-x").exists()
    assert client.get(f"/api/artifacts/{keep['id']}").status_code == 200
    assert client.get("/api/projects/acme/web/sessions/feature-x").status_code == 404


def test_delete_project(client, ctx):
    upload(client, b"one", "a.txt", "text/plain", project="acme/web", session="s1")
    upload(client, b"two", "b.txt", "text/plain", project="acme/web")
    other = upload(client, b"3", "c.txt", "text/plain", project="acme/other").json()
    preview = client.delete("/api/projects/acme/web", params={"dryRun": "true"}).json()
    assert preview["artifacts"] == 2 and preview["sessions"] == 1
    result = client.delete("/api/projects/acme/web").json()
    assert result["deleted"] is True
    assert projects_repo.get(ctx.db.conn(), "acme/web") is None
    assert not (ctx.paths.store_dir / "acme" / "web").exists()
    assert (ctx.paths.store_dir / "acme" / "other").exists()
    assert client.get(f"/api/artifacts/{other['id']}").status_code == 200
    assert client.get("/api/projects/acme/web").status_code == 404


def test_delete_is_atomic_when_rows_fail(client, ctx, monkeypatch):
    body = upload(client, b"x", "a.txt", "text/plain", project="acme/web", session="s").json()
    folder = artifact_dir(ctx, body["id"])

    def explode(conn, ids):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(artifacts_repo, "delete_many", explode)
    with pytest.raises(RuntimeError):
        from eks_harness.store import deletion
        deletion.delete_artifacts(ctx.db, ctx.paths, [body["id"]])
    assert folder.is_dir() and (folder / "a.txt").read_bytes() == b"x"
    assert artifacts_repo.get(ctx.db.conn(), body["id"]) is not None
    assert not [p for p in ctx.paths.tmp_dir.iterdir() if p.name.startswith("trash-")]


def _age(ctx, artifact_id: str, days: float) -> None:
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE artifacts SET created_at = ? WHERE id = ?", (time.time() - days * 86400, artifact_id))


def test_retention_deletes_old_unpinned_artifacts(client, ctx):
    old = upload(client, b"old", "old.txt", "text/plain", project="acme/web", session="s").json()
    pinned = upload(client, b"pin", "pin.txt", "text/plain", project="acme/web", session="s").json()
    fresh = upload(client, b"new", "new.txt", "text/plain", project="acme/web", session="s").json()
    untouched = upload(client, b"keep", "keep.txt", "text/plain", project="acme/forever").json()
    client.patch(f"/api/artifacts/{pinned['id']}", json={"pinned": True})
    client.patch("/api/projects/acme/web", json={"retentionDays": 7})
    for artifact_id in (old["id"], pinned["id"], untouched["id"]):
        _age(ctx, artifact_id, 10)
    report = retention.run_retention(ctx.db, ctx.config, events=ctx.events)
    assert report.deleted == 1 and report.projects == {"acme/web": 1} and report.bytes == 3
    remaining = {a["id"] for a in client.get("/api/artifacts").json()["items"]}
    assert remaining == {pinned["id"], fresh["id"], untouched["id"]}
    ctx.config.set("retention.defaultDays", 5)
    report = retention.run_retention(ctx.db, ctx.config)
    assert report.projects == {"acme/forever": 1}


def test_storage_usage_and_quota(client, ctx):
    upload(client, b"x" * 5000, "a.bin", project="acme/web")
    usage = retention.storage_usage(ctx.db, ctx.config, fresh=True)
    assert usage.artifact_count == 1
    assert usage.artifact_bytes == 5000
    assert usage.used_bytes >= 5000
    assert usage.quota_bytes is None and usage.over_quota is False
    assert usage.free_disk_bytes and usage.free_disk_bytes > 0
    ctx.config.set("storage.quotaGb", 1)
    usage = retention.storage_usage(ctx.db, ctx.config, fresh=True)
    assert usage.quota_bytes == 1024 ** 3 and usage.over_quota is False


def test_housekeeping_cleans_tmp_and_old_shares(client, ctx):
    stale = ctx.paths.tmp_dir / "stage-1-deadbeef"
    stale.mkdir(parents=True)
    (stale / "x").write_bytes(b"x")
    old = time.time() - 7 * 3600
    os.utime(stale, (old, old))
    recent = ctx.paths.tmp_dir / "upload-2-cafebabe"
    recent.mkdir()
    body = upload(client, b"x", "a.txt", "text/plain", project="acme/web").json()
    token = client.post(f"/api/artifacts/{body['id']}/shares", json={}).json()["token"]
    client.delete(f"/api/shares/{token}")
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE shares SET revoked_at = ? WHERE token = ?", (time.time() - 40 * 86400, token))
    report = retention.housekeeping(ctx.db, ctx.config)
    assert report["tmpRemoved"] == 1 and report["sharesPruned"] == 1
    assert not stale.exists() and recent.exists()
    assert shares_repo.get(ctx.db.conn(), token) is None
