from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from eks_harness.api.errors import ApiError
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import events as events_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store import layout
from test_store_support import (
    TINY_JPEG,
    TINY_MP4,
    add_lease,
    end_lease,
    local_path,
    needs_ffmpeg,
    needs_ffprobe,
    png_bytes,
    upload,
)
from conftest import ensure_session


def test_png_upload_stores_file_hash_and_dimensions(client, ctx):
    content = png_bytes(40, 30)
    response = upload(client, content, "shot.png", "image/png", project="acme/web", session="feature/login",
                      caption="Login page", tags="login, Smoke")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "screenshot"
    assert body["mime"] == "image/png"
    assert body["size"] == len(content)
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    assert (body["width"], body["height"]) == (40, 30)
    assert body["caption"] == "Login page"
    assert body["tags"] == ["login", "smoke"]
    assert body["sessionSlug"] == "feature-login"
    assert body["sessionName"] == "feature/login"
    assert body["url"] == f"http://127.0.0.1:7171/p/acme/web/s/feature-login/a/{body['id']}"
    assert body["rawUrl"] == f"http://127.0.0.1:7171/raw/{body['id']}/shot.png"
    assert body["downloadUrl"].endswith("?download=1")
    assert body["sessionUrl"] == "http://127.0.0.1:7171/p/acme/web/s/feature-login"
    stored = ctx.paths.store_dir / "acme" / "web" / "feature-login" / body["id"] / "shot.png"
    assert stored.read_bytes() == content
    project = projects_repo.get(ctx.db.conn(), "acme/web")
    assert project is not None and project.implicit


def test_links_use_public_url(client, ctx):
    ctx.config.set("server.publicUrl", "https://share.example.test")
    body = upload(client, png_bytes(), "a.png", "image/png", project="acme/web").json()
    assert body["url"].startswith("https://share.example.test/p/acme/web/s/_project/a/")
    assert body["rawUrl"].startswith("https://share.example.test/raw/")


@needs_ffmpeg
def test_thumbnail_generated_for_images(client):
    body = upload(client, png_bytes(900, 600), "big.png", "image/png", project="acme/web", session="s").json()
    assert body["thumbnailUrl"] == f"http://127.0.0.1:7171/thumb/{body['id']}.jpg"
    thumb = client.get(local_path(body["thumbnailUrl"]))
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    assert thumb.content.startswith(b"\xff\xd8")


def test_jpeg_dimensions_parsed_from_header(client):
    body = upload(client, TINY_JPEG, "photo.jpg", "image/jpeg", project="acme/web").json()
    assert body["kind"] == "screenshot"
    assert (body["width"], body["height"]) == (16, 16)


@needs_ffprobe
@needs_ffmpeg
def test_video_duration_and_thumbnail(client):
    body = upload(client, TINY_MP4, "flow.mp4", "video/mp4", project="acme/mobile", session="main").json()
    assert body["kind"] == "video"
    assert body["mime"] == "video/mp4"
    assert (body["width"], body["height"]) == (16, 16)
    assert 900 <= body["durationMs"] <= 1100
    assert body["thumbnailUrl"]


@pytest.mark.parametrize(("filename", "mime", "kind", "expected_kind", "expected_mime"), [
    ("network.har", "application/octet-stream", None, "har", "application/json"),
    ("page.mhtml", "application/octet-stream", None, "mhtml", "multipart/related"),
    ("dom.html", "text/html", None, "dom", "text/html"),
    ("device.log", "text/plain", None, "log", "text/plain"),
    ("accessibility.txt", "text/plain", None, "a11y", "text/plain"),
    ("blob.bin", "application/octet-stream", None, "file", "application/octet-stream"),
    ("output.txt", "text/plain", "console", "console", "text/plain"),
    ("notes.txt", "text/plain", "custom-kind", "custom-kind", "text/plain"),
])
def test_kind_inference(client, filename, mime, kind, expected_kind, expected_mime):
    content = b"{}" if filename.endswith(".har") else b"hello"
    response = upload(client, content, filename, mime, project="acme/web", kind=kind)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == expected_kind
    assert body["mime"] == expected_mime


def test_sniffed_mime_wins_over_declared(client):
    body = upload(client, png_bytes(), "capture.bin", "application/octet-stream", project="acme/web").json()
    assert body["kind"] == "screenshot"
    assert body["mime"] == "image/png"


def test_invalid_kind_and_meta_are_rejected(client):
    bad_kind = upload(client, b"x", "a.txt", "text/plain", project="acme/web", kind="Not Valid!")
    assert bad_kind.status_code == 400 and bad_kind.json()["error"] == "invalid_kind"
    bad_meta = upload(client, b"x", "a.txt", "text/plain", project="acme/web", meta="[1,2]")
    assert bad_meta.status_code == 400 and bad_meta.json()["error"] == "invalid_meta"
    site_kind = upload(client, b"x", "a.zip", "application/zip", project="acme/web", kind="site")
    assert site_kind.status_code == 400 and site_kind.json()["error"] == "use_site_upload"


def test_meta_and_source_are_stored(client):
    body = upload(client, b"x", "a.txt", "text/plain", project="acme/web", source="agent",
                  meta='{"url": "http://localhost:3000/x", "viewport": {"w": 1280}}').json()
    assert body["source"] == "agent"
    assert body["meta"] == {"url": "http://localhost:3000/x", "viewport": {"w": 1280}, "lines": 1}


def test_text_and_har_counts_are_recorded(client):
    text = upload(client, b"one\ntwo\nthree\n", "console.log", "text/plain", project="acme/web").json()
    assert text["kind"] == "console" and text["meta"]["lines"] == 3
    har = b'{"log": {"version": "1.2", "entries": [{"request": {}}, {"request": {}}]}}'
    counted = upload(client, har, "session.har", "application/json", project="acme/web").json()
    assert counted["kind"] == "har" and counted["meta"]["requests"] == 2
    image = upload(client, png_bytes(), "shot.png", "image/png", project="acme/web").json()
    assert "lines" not in image["meta"] and "requests" not in image["meta"]


def test_playlists_declared_as_video_are_never_probed(client, tmp_path):
    victim = tmp_path / "victim.mp4"
    victim.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    playlist = f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{victim}\n#EXT-X-ENDLIST\n".encode()
    body = upload(client, playlist, "evil.m3u8", "video/mp4", project="acme/web").json()
    assert body["width"] is None and body["durationMs"] is None and body["thumbnailUrl"] is None


def test_upload_size_limit(client, ctx):
    ctx.config.set("storage.maxUploadMb", 1)
    response = upload(client, b"0" * (1024 * 1024 + 10), "big.bin", project="acme/web")
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"
    assert artifacts_repo.count(ctx.db.conn(), artifacts_repo.ArtifactQuery()) == 0
    leftovers = [p for p in ctx.paths.tmp_dir.iterdir() if p.name.startswith(("upload-", "stage-"))]
    assert leftovers == []


def test_upload_request_shape_errors(client):
    two = client.post("/api/artifacts", data={"project": "acme/web"},
                      files=[("file", ("a.txt", b"a", "text/plain")), ("file", ("b.txt", b"b", "text/plain"))])
    assert two.status_code == 400 and two.json()["error"] == "one_file_per_upload"
    none = client.post("/api/artifacts", data={"project": "acme/web"},
                       files={"other": ("a.txt", b"a", "text/plain")})
    assert none.status_code == 400
    no_target = upload(client, b"a", "a.txt", "text/plain")
    assert no_target.status_code == 400 and no_target.json()["error"] == "no_target"
    bad_project = upload(client, b"a", "a.txt", "text/plain", project="Not A Project")
    assert bad_project.status_code == 400 and bad_project.json()["error"] == "invalid_project"
    not_multipart = client.post("/api/artifacts", json={"project": "acme/web"})
    assert not_multipart.status_code == 415


def test_filename_is_sanitized(client, ctx):
    body = upload(client, b"x", "../../etc/passwd", "text/plain", project="acme/web").json()
    assert body["filename"] == "passwd"
    reserved = upload(client, b"x", "site", "text/plain", project="acme/web").json()
    assert reserved["filename"] == "site_"
    hidden = upload(client, b"x", ".thumb.jpg", "text/plain", project="acme/web").json()
    assert hidden["filename"] == "thumb.jpg"
    for item in (body, reserved, hidden):
        artifact = artifacts_repo.get(ctx.db.conn(), item["id"])
        path = layout.file_path(ctx.paths, artifact.rel_path)
        assert path.is_relative_to(ctx.paths.store_dir.resolve())


def test_project_level_upload(client):
    body = upload(client, b"x", "readme.txt", "text/plain", project="acme/web").json()
    assert body["sessionId"] is None
    assert body["sessionSlug"] is None
    assert "/s/_project/a/" in body["url"]
    assert body["sessionUrl"] == "http://127.0.0.1:7171/p/acme/web"


def test_sid_upload_and_released_sid_is_gone(client, ctx):
    session = ensure_session(ctx.db, "acme/mobile-app", "feature/yardim-merkezi")
    lease = add_lease(ctx.db, session.id)
    body = upload(client, png_bytes(), "s.png", "image/png", sid=lease.sid).json()
    assert body["leaseSid"] == lease.sid
    assert body["projectId"] == "acme/mobile-app"
    assert body["sessionSlug"] == "feature-yardim-merkezi"
    mismatch = upload(client, png_bytes(), "s.png", "image/png", sid=lease.sid, project="other/project")
    assert mismatch.status_code == 400 and mismatch.json()["error"] == "sid_project_mismatch"
    end_lease(ctx.db, lease)
    released = upload(client, png_bytes(), "s.png", "image/png", sid=lease.sid)
    assert released.status_code == 410
    payload = released.json()
    assert payload["error"] == "lease_released"
    assert payload["project"] == "acme/mobile-app"
    assert payload["reacquire"].startswith("eks-harness lease acquire --project acme/mobile-app")
    unknown = upload(client, png_bytes(), "s.png", "image/png", sid="zzzzzz")
    assert unknown.status_code == 404 and unknown.json()["error"] == "sid_not_found"


def test_ingest_file_python_api(ctx, tmp_path):
    source = tmp_path / "capture.png"
    content = png_bytes(8, 6)
    source.write_bytes(content)
    session = ensure_session(ctx.db, "acme/web", "main")
    lease = add_lease(ctx.db, session.id, kind="browser", resource="browser-1-1")
    artifact = store_artifacts.ingest_file(ctx.db, ctx.config, sid=lease.sid, path_or_stream=source,
                                           caption="From the pool", tags=["auto"], meta={"device": "ios-1"},
                                           source="agent", user="pool", events=ctx.events)
    assert source.exists()
    assert artifact.lease_sid == lease.sid
    assert artifact.session_id == session.id
    assert (artifact.width, artifact.height) == (8, 6)
    assert artifact.created_by == "pool"
    assert artifact.meta["device"] == "ios-1"
    assert layout.file_path(ctx.paths, artifact.rel_path).read_bytes() == content
    recorded = events_repo.list_events(ctx.db.conn(), types=["artifact.created"])
    assert recorded and recorded[0].detail["id"] == artifact.id
    assert recorded[0].lease_sid == lease.sid and recorded[0].resource == "browser-1-1"

    moved = tmp_path / "moved.log"
    moved.write_text("line 1\n")
    from_move = store_artifacts.ingest_file(ctx.db, ctx.config, project="acme/web", session="main",
                                            path_or_stream=moved, move=True, kind="log")
    assert not moved.exists()
    assert from_move.kind == "log"

    from_bytes = store_artifacts.ingest_file(ctx.db, ctx.config, project="acme/web", path_or_stream=b"abc",
                                             kind="console")
    assert from_bytes.filename == "console.log" and from_bytes.size == 3

    from_stream = store_artifacts.ingest_file(ctx.db, ctx.config, project="new/project", session="s",
                                              path_or_stream=io.BytesIO(b"{}"), filename="run.har")
    assert from_stream.kind == "har"
    assert projects_repo.get(ctx.db.conn(), "new/project").implicit

    with pytest.raises(ApiError) as too_big:
        store_artifacts.ingest_file(ctx.db, ctx.config, project="acme/web", path_or_stream=b"x" * 100,
                                    max_bytes=10)
    assert too_big.value.status == 413

    end_lease(ctx.db, lease)
    with pytest.raises(ApiError) as released:
        store_artifacts.ingest_file(ctx.db, ctx.config, sid=lease.sid, path_or_stream=b"x")
    assert released.value.status == 410
    leftovers = [p for p in ctx.paths.tmp_dir.iterdir() if p.name.startswith("stage-")]
    assert leftovers == []


def test_ingest_file_checks_principal_grants(auth_env):
    from eks_harness.auth.core import Principal

    user, _ = auth_env.make_user("viewer-user")
    auth_env.grant(user, "acme/web", "viewer")
    principal = Principal(user_id=user.id, username=user.username, role="member", via="key")
    ctx = auth_env.app.state.ctx
    with pytest.raises(ApiError) as denied:
        store_artifacts.ingest_file(ctx.db, ctx.config, project="acme/web", path_or_stream=b"x", user=principal)
    assert denied.value.status == 403
    with pytest.raises(ApiError) as hidden:
        store_artifacts.ingest_file(ctx.db, ctx.config, project="other/place", path_or_stream=b"x", user=principal)
    assert hidden.value.status == 404


def test_artifact_created_event_reaches_bus(client, ctx):
    received = []
    remove = ctx.events.add_listener(received.append)
    try:
        body = upload(client, b"x", "a.txt", "text/plain", project="acme/web", session="s1").json()
    finally:
        remove()
    created = [e for e in received if e.type == "artifact.created"]
    assert created and created[0].detail["id"] == body["id"]
    assert created[0].project_id == "acme/web"
    assert any(e.type == "project.updated" for e in received)


def test_stored_path_layout(client, ctx):
    body = upload(client, b"x", "a.txt", "text/plain", project="acme/web", session="Feature X").json()
    artifact = artifacts_repo.get(ctx.db.conn(), body["id"])
    assert artifact.rel_path == f"acme/web/feature-x/{body['id']}/a.txt"
    assert Path(layout.file_path(ctx.paths, artifact.rel_path)).is_file()
