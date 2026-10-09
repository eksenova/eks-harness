from __future__ import annotations

import io
import zipfile

import pytest

from eks_harness.auth.core import COOKIE_NAME, create_web_session
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.store.serving import SITE_CSP, site_csp
from test_store_support import chrome_mhtml, local_path, make_zip, png_bytes, upload

SITE = {
    "index.html": b"<!doctype html><link rel=stylesheet href=css/app.css><script src=app.js></script><h1>Hi</h1>",
    "css/app.css": b"h1{color:red}",
    "app.js": b"console.log(1)",
    "docs/index.html": b"<p>docs</p>",
    "img/logo.png": png_bytes(4, 4),
}


def post_zip(client, content: bytes, name: str = "report.zip", **fields):
    data = {"project": "acme/web", "session": "s", **{k: v for k, v in fields.items() if v is not None}}
    return client.post("/api/sites", data=data, files={"file": (name, content, "application/zip")})


def test_zip_site_upload_and_serving(client, ctx):
    response = post_zip(client, make_zip(SITE), caption="Coverage report")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "site"
    assert body["filename"] == "report.zip"
    assert body["mime"] == "application/zip"
    assert body["siteUrl"] == f"http://127.0.0.1:7171/site/{body['id']}/index.html"
    assert body["meta"]["site"]["entry"] == "index.html"
    assert sorted(body["meta"]["site"]["files"]) == sorted(SITE)
    page = client.get(local_path(body["siteUrl"]))
    assert page.status_code == 200
    assert page.content == SITE["index.html"]
    assert page.headers["content-security-policy"] == SITE_CSP
    assert "allow-same-origin" not in page.headers["content-security-policy"]
    assert page.headers["access-control-allow-origin"] == "*"
    assert page.headers["content-type"] == "text/html"
    css = client.get(f"/site/{body['id']}/css/app.css")
    assert css.headers["content-type"] == "text/css; charset=utf-8"
    assert css.headers["content-security-policy"] == SITE_CSP
    js = client.get(f"/site/{body['id']}/app.js")
    assert js.headers["content-type"].startswith("text/javascript")
    image = client.get(f"/site/{body['id']}/img/logo.png")
    assert image.headers["content-type"] == "image/png"
    assert image.content == SITE["img/logo.png"]
    root = client.get(f"/site/{body['id']}", follow_redirects=False)
    assert root.status_code == 302 and root.headers["location"] == f"/site/{body['id']}/index.html"
    folder = client.get(f"/site/{body['id']}/docs", follow_redirects=False)
    assert folder.status_code == 302 and folder.headers["location"] == f"/site/{body['id']}/docs/"
    assert client.get(f"/site/{body['id']}/docs/").content == b"<p>docs</p>"
    missing = client.get(f"/site/{body['id']}/nope.html")
    assert missing.status_code == 404
    assert missing.headers["content-security-policy"] == SITE_CSP
    escape = client.get(f"/site/{body['id']}/..%2F..%2Fsecret")
    assert escape.status_code == 404
    raw = client.get(local_path(body["rawUrl"]))
    assert raw.headers["content-disposition"].startswith("attachment;")
    assert zipfile.ZipFile(io.BytesIO(raw.content)).read("index.html") == SITE["index.html"]
    listed = client.get("/api/artifacts", params={"kind": "site"}).json()["items"][0]
    assert listed["meta"]["site"]["fileCount"] == len(SITE) and "files" not in listed["meta"]["site"]
    detail = client.get(f"/api/artifacts/{body['id']}").json()
    assert len(detail["meta"]["site"]["files"]) == len(SITE)


def test_zip_with_single_top_folder_is_flattened(client):
    nested = make_zip({f"build/{k}": v for k, v in SITE.items()})
    body = post_zip(client, nested).json()
    assert body["meta"]["site"]["entry"] == "index.html"
    assert client.get(f"/site/{body['id']}/css/app.css").content == SITE["css/app.css"]


def test_custom_entry(client):
    body = post_zip(client, make_zip({"report/main.html": b"<p>main</p>"}), entry="report/main.html").json()
    assert body["siteUrl"].endswith("/report/main.html")
    missing = post_zip(client, make_zip({"a.html": b"a"}), entry="index.html")
    assert missing.status_code == 400
    assert missing.json()["error"] == "entry_missing"
    assert "a.html" in missing.json()["message"]


@pytest.mark.parametrize("entries", [
    {"../evil.html": b"x", "index.html": b"ok"},
    {"a/../../evil.html": b"x", "index.html": b"ok"},
    {"/etc/evil.html": b"x", "index.html": b"ok"},
    {"C:/evil.html": b"x", "index.html": b"ok"},
    {"..\\evil.html": b"x", "index.html": b"ok"},
])
def test_zip_slip_is_rejected(client, ctx, entries):
    response = post_zip(client, make_zip(entries))
    assert response.status_code == 400
    assert response.json()["error"] == "unsafe_path"
    assert artifacts_repo.count(ctx.db.conn(), artifacts_repo.ArtifactQuery()) == 0
    assert not (ctx.paths.data_dir / "evil.html").exists()
    assert not list(ctx.paths.data_dir.rglob("evil.html"))


def test_zip_symlink_is_rejected(client):
    response = post_zip(client, make_zip({"index.html": b"ok"}, symlinks={"link": "/etc/passwd"}))
    assert response.status_code == 400 and response.json()["error"] == "unsafe_path"


def test_bad_zip_and_expansion_limit(client, ctx):
    assert post_zip(client, b"not a zip").json()["error"] == "bad_zip"
    ctx.config.set("storage.maxUploadMb", 1)
    bomb = make_zip({"index.html": b"<p>x</p>", "big.bin": b"\0" * (5 * 1024 * 1024)})
    response = post_zip(client, bomb)
    assert response.status_code == 400 and response.json()["error"] == "site_too_large"


def test_files_upload_with_relative_paths(client):
    files = [("files", (f"mysite/{name}", data, "application/octet-stream")) for name, data in SITE.items()]
    response = client.post("/api/sites", data={"project": "acme/web", "session": "s"}, files=files)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "mysite.zip"
    assert client.get(f"/site/{body['id']}/css/app.css").content == SITE["css/app.css"]
    raw = client.get(local_path(body["rawUrl"]))
    assert sorted(zipfile.ZipFile(io.BytesIO(raw.content)).namelist()) == sorted(SITE)


def test_files_upload_with_paths_field(client):
    files = [("files", ("blob", b"<p>x</p>", "text/html")), ("files", ("blob", b"a{}", "text/css"))]
    response = client.post("/api/sites", data={"project": "acme/web", "paths": ["index.html", "s/a.css"]},
                           files=files)
    assert response.status_code == 201, response.text
    assert client.get(f"/site/{response.json()['id']}/s/a.css").content == b"a{}"


def test_files_upload_rejects_traversal(client):
    files = [("files", ("index.html", b"x", "text/html")), ("files", ("../../x.html", b"x", "text/html"))]
    response = client.post("/api/sites", data={"project": "acme/web"}, files=files)
    assert response.status_code == 400 and response.json()["error"] == "unsafe_path"


def test_mhtml_is_unpacked_into_a_site(client):
    content = chrome_mhtml()
    body = upload(client, content, "page.mhtml", "multipart/related", project="acme/web", session="s").json()
    assert body["kind"] == "mhtml"
    site = body["meta"]["site"]
    assert site["entry"] == "index.html"
    assert site["source"] == "mhtml"
    assert site["resources"] == 3
    assert body["meta"]["url"] == "https://app.example.test/dashboard/"
    page = client.get(local_path(body["siteUrl"]))
    assert page.headers["content-security-policy"] == site_csp("mhtml")
    assert page.headers["content-security-policy"].startswith(SITE_CSP + ";")
    assert "script-src 'none'" in page.headers["content-security-policy"]
    html = page.content.decode("utf-8")
    assert "Dashboard \u00e7" in html
    assert "<base" not in html
    assert 'href="res/0001-style.css"' in html
    assert 'src="res/0002-logo.png"' in html
    assert "res/0002-logo.png 1x, res/0002-logo.png 2x" in html
    assert "url(&quot;res/0003-bg.png&quot;)" in html
    assert 'href="https://elsewhere.test/page"' in html
    css = client.get(f"/site/{body['id']}/res/0001-style.css").text
    assert 'url("0003-bg.png")' in css
    logo = client.get(f"/site/{body['id']}/res/0002-logo.png")
    assert logo.status_code == 200 and logo.content == png_bytes(4, 4)
    raw = client.get(local_path(body["rawUrl"]))
    assert raw.content == content
    assert raw.headers["content-disposition"].startswith("attachment;")


def test_broken_mhtml_is_kept_without_site(client):
    body = upload(client, b"not mime at all", "broken.mhtml", "multipart/related", project="acme/web").json()
    assert body["kind"] == "mhtml"
    assert body["siteUrl"] is None
    assert "siteError" in body["meta"]


def test_dom_dump_gets_a_base_tag(client):
    body = upload(client, b"<html><head><title>x</title></head><body>dom</body></html>", "dom.html", "text/html",
                  project="acme/web", meta='{"url": "http://localhost:3000/panel"}').json()
    assert body["kind"] == "dom"
    page = client.get(local_path(body["siteUrl"])).text
    assert '<head><base href="http://localhost:3000/panel">' in page
    assert client.get(local_path(body["rawUrl"])).headers["content-disposition"].startswith("attachment")


def test_site_access_with_auth_uses_scoped_tokens(auth_env):
    client = auth_env.client
    response = client.post("/api/sites", data={"project": "acme/web"},
                           files={"file": ("r.zip", make_zip(SITE), "application/zip")},
                           headers=auth_env.admin_headers)
    assert response.status_code == 201, response.text
    site_id = response.json()["id"]
    assert client.get(f"/site/{site_id}/index.html").status_code == 401
    direct = client.get(f"/site/{site_id}/index.html", headers=auth_env.admin_headers)
    assert direct.status_code == 200 and direct.content == SITE["index.html"]

    token, _ = create_web_session(auth_env.db, auth_env.admin.id, 1)
    client.cookies.set(COOKIE_NAME, token)
    redirect = client.get(f"/site/{site_id}/css/app.css", follow_redirects=False)
    assert redirect.status_code == 302
    location = redirect.headers["location"]
    assert location.startswith(f"/site/{site_id}/~t.")
    assert location.endswith("/css/app.css")
    client.cookies.clear()
    via_token = client.get(location)
    assert via_token.status_code == 200 and via_token.content == SITE["css/app.css"]
    assert via_token.headers["content-security-policy"] == SITE_CSP
    token_segment = location.split("/")[3]
    other = client.post("/api/sites", data={"project": "acme/web"},
                        files={"file": ("r.zip", make_zip(SITE), "application/zip")},
                        headers=auth_env.admin_headers).json()["id"]
    assert client.get(f"/site/{other}/{token_segment}/index.html").status_code == 401
    forged = token_segment[:-4] + "AAAA"
    assert client.get(f"/site/{site_id}/{forged}/index.html").status_code == 401
