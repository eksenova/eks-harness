from __future__ import annotations

import re
import shutil
import time

import pytest

from eks_harness.db.repos import shares as shares_repo
from eks_harness.store.serving import SITE_CSP
from test_store_support import TINY_MP4, local_path, make_zip, png_bytes, upload


def admin_upload(env, content: bytes, filename: str, mime: str, **fields):
    response = upload(env.client, content, filename, mime, headers=env.admin_headers, **fields)
    assert response.status_code == 201, response.text
    return response.json()


def create_share(env, artifact_id: str, headers=None, **body):
    return env.client.post(f"/api/artifacts/{artifact_id}/shares", json=body, headers=headers or env.admin_headers)


def test_share_link_serves_one_artifact_without_login(auth_env):
    image = admin_upload(auth_env, png_bytes(), "shot.png", "image/png", project="acme/web", session="s")
    other = admin_upload(auth_env, b"secret", "other.txt", "text/plain", project="acme/web", session="s")
    response = create_share(auth_env, image["id"])
    assert response.status_code == 201, response.text
    share = response.json()
    token = share["token"]
    assert len(token) == 32
    assert share["url"] == f"http://127.0.0.1:7171/s/{token}"
    assert share["rawUrl"] == f"http://127.0.0.1:7171/s/{token}/raw"
    assert share["directUrl"] == f"http://127.0.0.1:7171/d/{token}/{image['filename']}"
    assert share["expiresAt"] is None and share["active"] is True

    client = auth_env.client
    assert client.get(local_path(image["rawUrl"])).status_code == 401
    raw = client.get(f"/s/{token}/raw")
    assert raw.status_code == 200
    assert raw.content == png_bytes()
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert raw.headers["content-disposition"].startswith("inline")
    download = client.get(f"/s/{token}/raw", params={"download": "1"})
    assert download.headers["content-disposition"].startswith("attachment")
    assert client.get(f"/s/{token}/{other['id']}").status_code == 404
    assert client.get(f"/s/{token}/raw%2F..%2F{other['id']}").status_code == 404

    info = client.get(f"/api/shared/{token}")
    assert info.status_code == 200
    payload = info.json()
    assert payload["artifact"]["filename"] == image["filename"]
    assert payload["artifact"]["rawUrl"] == share["rawUrl"]
    assert payload["artifact"]["url"] == share["url"]
    assert set(payload["artifact"]) == {"kind", "mime", "filename", "size", "width", "height", "durationMs",
                                        "caption", "createdAt", "url", "rawUrl", "directUrl", "downloadUrl", "siteUrl",
                                        "meta"}
    assert payload["artifact"]["directUrl"] == share["directUrl"]
    assert payload["artifact"]["meta"] == {}

    as_json = client.get(f"/s/{token}", headers={"Accept": "application/json"})
    assert as_json.json()["artifact"]["filename"] == image["filename"]
    assert "projectId" not in as_json.json()["artifact"]


def test_share_page_and_view_counting(auth_env, tmp_path, monkeypatch):
    image = admin_upload(auth_env, png_bytes(), "shot.png", "image/png", project="acme/web")
    token = create_share(auth_env, image["id"]).json()["token"]
    client = auth_env.client
    page = client.get(f"/s/{token}")
    assert page.status_code == 200
    assert "shot.png" in page.text
    assert f'src="/s/{token}/raw"' in page.text
    assert page.headers["referrer-policy"] == "no-referrer"
    client.get(f"/s/{token}/raw", headers={"Sec-Fetch-Dest": "image"})
    client.get(f"/s/{token}/raw", headers={"Range": "bytes=10-20"})
    client.get(f"/s/{token}/raw")
    listed = client.get(f"/api/artifacts/{image['id']}/shares", headers=auth_env.admin_headers).json()["items"]
    assert listed[0]["views"] == 2
    assert listed[0]["lastViewedAt"] is not None

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    monkeypatch.setenv("EKS_HARNESS_WEB_DIST", str(dist))
    spa = client.get(f"/s/{token}")
    assert spa.text == "<!doctype html><div id=root></div>"
    (dist / "index.html").write_text("<!doctype html><html><head><title>eks-harness</title></head>"
                                     "<body><div id=root></div></body></html>")
    spa = client.get(f"/s/{token}")
    assert "<title>shot.png</title>" in spa.text and "eks-harness</title>" not in spa.text
    assert f'<meta property="og:image" content="http://127.0.0.1:7171/t/{token}.jpg">' in spa.text
    assert '<div id=root></div>' in spa.text
    assert spa.headers["cache-control"] == "no-store"


def test_share_expiry(auth_env):
    image = admin_upload(auth_env, b"x", "a.txt", "text/plain", project="acme/web")
    share = create_share(auth_env, image["id"], expires="1h").json()
    assert share["expiresAt"] is not None
    token = share["token"]
    assert auth_env.client.get(f"/s/{token}/raw").status_code == 200
    with auth_env.db.transaction() as conn:
        conn.execute("UPDATE shares SET expires_at = ? WHERE token = ?", (time.time() - 1, token))
    expired = auth_env.client.get(f"/s/{token}/raw")
    assert expired.status_code == 410 and expired.json()["error"] == "share_expired"
    page = auth_env.client.get(f"/s/{token}")
    assert page.status_code == 410 and "expired" in page.text
    listed = auth_env.client.get(f"/api/artifacts/{image['id']}/shares", headers=auth_env.admin_headers).json()
    assert listed["items"][0]["active"] is False


@pytest.mark.parametrize("expires", ["1d", "7d", "30d", "90m", "2w", "never"])
def test_share_expiry_presets(auth_env, expires):
    image = admin_upload(auth_env, b"x", "a.txt", "text/plain", project="acme/web")
    share = create_share(auth_env, image["id"], expires=expires)
    assert share.status_code == 201
    assert (share.json()["expiresAt"] is None) == (expires == "never")


def test_invalid_expiry(auth_env):
    image = admin_upload(auth_env, b"x", "a.txt", "text/plain", project="acme/web")
    assert create_share(auth_env, image["id"], expires="soon").status_code == 422
    assert create_share(auth_env, image["id"], expiresAt="2001-01-01T00:00:00Z").status_code == 400


def test_revoke(auth_env):
    image = admin_upload(auth_env, b"x", "a.txt", "text/plain", project="acme/web")
    token = create_share(auth_env, image["id"]).json()["token"]
    revoked = auth_env.client.delete(f"/api/shares/{token}", headers=auth_env.admin_headers)
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False and revoked.json()["revokedAt"] is not None
    gone = auth_env.client.get(f"/s/{token}/raw")
    assert gone.status_code == 410 and gone.json()["error"] == "share_revoked"
    assert auth_env.client.get("/s/" + "x" * 32).status_code == 404
    assert auth_env.client.delete("/api/shares/" + "x" * 32, headers=auth_env.admin_headers).status_code == 404
    assert shares_repo.get(auth_env.db.conn(), token).revoked_at is not None


def test_share_of_a_site_serves_its_files_sandboxed(auth_env):
    response = auth_env.client.post(
        "/api/sites", data={"project": "acme/web"}, headers=auth_env.admin_headers,
        files={"file": ("r.zip", make_zip({"index.html": b"<p>hi</p>", "a/b.css": b"x{}"}), "application/zip")})
    site = response.json()
    share = create_share(auth_env, site["id"]).json()
    token = share["token"]
    info = auth_env.client.get(f"/api/shared/{token}").json()
    assert info["artifact"]["siteUrl"] == f"http://127.0.0.1:7171/s/{token}/index.html"
    page = auth_env.client.get(f"/s/{token}/index.html")
    assert page.status_code == 200 and page.content == b"<p>hi</p>"
    assert page.headers["content-security-policy"] == SITE_CSP
    assert page.headers["access-control-allow-origin"] == "*"
    assert auth_env.client.get(f"/s/{token}/a/b.css").content == b"x{}"
    assert auth_env.client.get(f"/s/{token}/..%2F..%2Fetc%2Fpasswd").status_code == 404
    redirect = auth_env.client.get(f"/s/{token}/", follow_redirects=False)
    assert redirect.status_code == 302 and redirect.headers["location"] == f"/s/{token}/index.html"
    raw = auth_env.client.get(f"/s/{token}/raw")
    assert raw.headers["content-disposition"].startswith("attachment")


def test_share_permissions(auth_env):
    video = admin_upload(auth_env, TINY_MP4, "v.mp4", "video/mp4", project="acme/web")
    viewer, viewer_key = auth_env.make_user("viewer")
    editor, editor_key = auth_env.make_user("editor")
    auth_env.grant(viewer, "acme/web", "viewer")
    auth_env.grant(editor, "acme/web", "editor")
    denied = create_share(auth_env, video["id"], headers=auth_env.headers_for(viewer_key))
    assert denied.status_code == 403
    created = create_share(auth_env, video["id"], headers=auth_env.headers_for(editor_key))
    assert created.status_code == 201
    token = created.json()["token"]
    stranger, stranger_key = auth_env.make_user("stranger")
    assert auth_env.client.delete(f"/api/shares/{token}", headers=auth_env.headers_for(stranger_key)).status_code == 404
    assert auth_env.client.delete(f"/api/shares/{token}", headers=auth_env.headers_for(viewer_key)).status_code == 403
    assert auth_env.client.delete(f"/api/shares/{token}", headers=auth_env.headers_for(editor_key)).status_code == 200
    detail = auth_env.client.get(f"/api/artifacts/{video['id']}", headers=auth_env.admin_headers).json()
    assert detail["shareCount"] == 0


def test_deleted_artifact_share_is_gone(auth_env):
    image = admin_upload(auth_env, b"x", "a.txt", "text/plain", project="acme/web")
    token = create_share(auth_env, image["id"]).json()["token"]
    auth_env.client.delete(f"/api/artifacts/{image['id']}", headers=auth_env.admin_headers)
    assert auth_env.client.get(f"/s/{token}/raw").status_code == 404


def test_direct_link_serves_the_file_itself_without_login(auth_env):
    image = admin_upload(auth_env, png_bytes(), "login screen.png", "image/png", project="acme/web", session="s")
    share = create_share(auth_env, image["id"]).json()
    token = share["token"]
    assert share["directUrl"] == f"http://127.0.0.1:7171/d/{token}/login%20screen.png"
    client = auth_env.client
    direct = client.get(local_path(share["directUrl"]), headers={"Accept": "text/html"})
    assert direct.status_code == 200
    assert direct.content == png_bytes()
    assert direct.headers["content-type"] == "image/png"
    assert direct.headers["content-disposition"].startswith("inline")
    assert direct.headers["cache-control"] == "private, no-cache"
    assert direct.headers["cross-origin-resource-policy"] == "cross-origin"
    assert direct.headers["referrer-policy"] == "no-referrer"
    download = client.get(local_path(share["directUrl"]), params={"download": "1"})
    assert download.headers["content-disposition"].startswith("attachment")
    bare = client.get(f"/d/{token}", follow_redirects=False)
    assert bare.status_code == 302 and bare.headers["location"] == f"/d/{token}/login%20screen.png"
    slash = client.get(f"/d/{token}/", follow_redirects=False)
    assert slash.status_code == 302 and slash.headers["location"] == f"/d/{token}/login%20screen.png"
    listed = client.get(f"/api/artifacts/{image['id']}/shares", headers=auth_env.admin_headers).json()["items"]
    assert listed[0]["views"] == 2
    assert listed[0]["directUrl"] == share["directUrl"]


def test_direct_link_streams_video_ranges(auth_env):
    video = admin_upload(auth_env, TINY_MP4, "flow.mp4", "video/mp4", project="acme/web")
    share = create_share(auth_env, video["id"]).json()
    path = local_path(share["directUrl"])
    assert path.endswith("/flow.mp4")
    part = auth_env.client.get(path, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206
    assert part.content == TINY_MP4[:10]
    assert part.headers["content-type"] == "video/mp4"


def test_direct_link_of_a_site_serves_the_site(auth_env):
    response = auth_env.client.post(
        "/api/sites", data={"project": "acme/web"}, headers=auth_env.admin_headers,
        files={"file": ("r.zip", make_zip({"index.html": b"<p>hi</p>", "a/b.css": b"x{}"}), "application/zip")})
    site = response.json()
    share = create_share(auth_env, site["id"]).json()
    token = share["token"]
    assert share["directUrl"] == f"http://127.0.0.1:7171/d/{token}/index.html"
    page = auth_env.client.get(f"/d/{token}/index.html")
    assert page.status_code == 200 and page.content == b"<p>hi</p>"
    assert page.headers["content-security-policy"] == SITE_CSP
    assert auth_env.client.get(f"/d/{token}/a/b.css").content == b"x{}"
    assert auth_env.client.get(f"/d/{token}/..%2F..%2Fetc%2Fpasswd").status_code == 404
    redirect = auth_env.client.get(f"/d/{token}/", follow_redirects=False)
    assert redirect.status_code == 302 and redirect.headers["location"] == f"/d/{token}/index.html"
    download = auth_env.client.get(f"/d/{token}/index.html", params={"download": "1"})
    assert download.headers["content-disposition"].startswith("attachment")


def test_direct_link_of_a_dom_snapshot_serves_it_sandboxed(auth_env):
    dom = admin_upload(auth_env, b"<html><body><p>snap</p><script>x()</script></body></html>", "page.html",
                       "text/html", project="acme/web", kind="dom")
    share = create_share(auth_env, dom["id"]).json()
    page = auth_env.client.get(local_path(share["directUrl"]))
    assert page.status_code == 200
    assert b"snap" in page.content
    assert "script-src 'none'" in page.headers["content-security-policy"]


def test_direct_link_revoked_or_unknown(auth_env):
    image = admin_upload(auth_env, png_bytes(), "shot.png", "image/png", project="acme/web")
    share = create_share(auth_env, image["id"]).json()
    token = share["token"]
    auth_env.client.delete(f"/api/shares/{token}", headers=auth_env.admin_headers)
    path = local_path(share["directUrl"])
    assert auth_env.client.get(path).status_code == 410
    page = auth_env.client.get(path, headers={"Accept": "text/html"})
    assert page.status_code == 410
    assert "Share link unavailable" in page.text and "revoked" in page.text
    navigated = auth_env.client.get(path, headers={"Accept": "image/webp,*/*", "Sec-Fetch-Dest": "document"})
    assert navigated.status_code == 410 and "Share link unavailable" in navigated.text
    assert auth_env.client.get("/d/" + "x" * 32 + "/shot.png").status_code == 404
    assert auth_env.client.get("/d/nope/shot.png").status_code == 404


SLACKBOT = "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)"
DISCORDBOT = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0"
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is needed")


def meta(page: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r'<meta (?:property|name)="([^"]+)" content="([^"]*)">', page)}


def test_share_page_carries_link_preview_tags(auth_env):
    image = admin_upload(auth_env, png_bytes(), "shot.png", "image/png", project="acme/web",
                         caption='Login "error" <b>\nSecond line')
    token = create_share(auth_env, image["id"]).json()["token"]
    page = auth_env.client.get(f"/s/{token}", headers={"User-Agent": SLACKBOT})
    tags = meta(page.text)
    assert tags["og:title"] == "Login &quot;error&quot; &lt;b&gt;"
    assert tags["og:url"] == f"http://127.0.0.1:7171/s/{token}"
    assert tags["og:image"] == f"http://127.0.0.1:7171/t/{token}.jpg"
    assert tags["og:image:type"] == "image/jpeg"
    assert tags["twitter:card"] == "summary_large_image"
    assert tags["og:type"] == "website"
    assert tags["og:site_name"] == "eks-harness"
    assert "Second line" in tags["og:description"] and "shot.png" in tags["og:description"]
    assert "<b>" not in page.text
    assert "og:video" not in tags
    listed = auth_env.client.get(f"/api/artifacts/{image['id']}/shares", headers=auth_env.admin_headers).json()
    assert listed["items"][0]["views"] == 0


def test_video_share_preview_offers_the_stream(auth_env):
    video = admin_upload(auth_env, TINY_MP4, "flow.mp4", "video/mp4", project="acme/web")
    token = create_share(auth_env, video["id"]).json()["token"]
    tags = meta(auth_env.client.get(f"/s/{token}").text)
    assert tags["og:type"] == "video.other"
    assert tags["og:video"] == tags["og:video:secure_url"] == f"http://127.0.0.1:7171/s/{token}/raw"
    assert tags["og:video:type"] == "video/mp4"
    assert tags["og:image"] == f"http://127.0.0.1:7171/t/{token}.jpg"


def test_text_share_preview_is_a_summary_card(auth_env):
    text = admin_upload(auth_env, b"hello", "notes.txt", "text/plain", project="acme/web")
    token = create_share(auth_env, text["id"]).json()["token"]
    tags = meta(auth_env.client.get(f"/s/{token}").text)
    assert tags["twitter:card"] == "summary"
    assert "og:image" not in tags
    assert tags["og:title"] == "notes.txt"
    assert auth_env.client.get(f"/t/{token}.jpg").status_code == 404


def test_direct_link_gives_preview_bots_a_card_and_people_the_file(auth_env):
    image = admin_upload(auth_env, png_bytes(), "shot.png", "image/png", project="acme/web")
    share = create_share(auth_env, image["id"]).json()
    path = local_path(share["directUrl"])
    for agent in (SLACKBOT, DISCORDBOT, "facebookexternalhit/1.1 Facebot Twitterbot/1.0"):
        card = auth_env.client.get(path, headers={"User-Agent": agent})
        assert card.status_code == 200 and card.headers["content-type"].startswith("text/html")
        assert card.headers["cache-control"] == "no-store"
        tags = meta(card.text)
        assert tags["og:url"] == share["directUrl"]
        assert tags["og:image"] == f"http://127.0.0.1:7171/t/{share['token']}.jpg"
    person = auth_env.client.get(path, headers={"User-Agent": BROWSER})
    assert person.content == png_bytes()
    raw_for_bot = auth_env.client.get(f"/s/{share['token']}/raw", headers={"User-Agent": DISCORDBOT})
    assert raw_for_bot.content == png_bytes()
    listed = auth_env.client.get(f"/api/artifacts/{image['id']}/shares", headers=auth_env.admin_headers).json()
    assert listed["items"][0]["views"] == 1


@needs_ffmpeg
def test_preview_image_is_a_public_jpeg(auth_env):
    image = admin_upload(auth_env, png_bytes(), "shot.png", "image/png", project="acme/web")
    token = create_share(auth_env, image["id"]).json()["token"]
    poster = auth_env.client.get(f"/t/{token}.jpg", headers={"User-Agent": SLACKBOT})
    assert poster.status_code == 200
    assert poster.headers["content-type"] == "image/jpeg"
    assert poster.content[:3] == b"\xff\xd8\xff"
    assert poster.headers["cross-origin-resource-policy"] == "cross-origin"
    assert auth_env.client.get(f"/t/{token}.jpg").content == poster.content


def test_share_errors_are_never_cached(auth_env):
    for path in ("/d/" + "x" * 32 + "/shot.png", "/t/" + "x" * 32 + ".jpg", "/s/" + "x" * 32 + "/raw",
                 "/s/" + "x" * 32 + "/a.png"):
        response = auth_env.client.get(path)
        assert response.status_code == 404, path
        assert response.headers["cache-control"] == "no-store", path
