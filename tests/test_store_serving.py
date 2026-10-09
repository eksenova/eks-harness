from __future__ import annotations

import io
import zipfile

import pytest

from test_store_support import TINY_MP4, local_path, png_bytes, upload


@pytest.fixture
def video(client):
    body = upload(client, TINY_MP4, "flow.mp4", "video/mp4", project="acme/web", session="s").json()
    return body, local_path(body["rawUrl"])


def test_full_response_headers(client, video):
    body, path = video
    response = client.get(path)
    assert response.status_code == 200
    assert response.content == TINY_MP4
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-length"] == str(len(TINY_MP4))
    assert response.headers["etag"] == f'"{body["sha256"][:40]}"'
    assert response.headers["cache-control"].startswith("private")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith('inline; filename="flow.mp4"')


@pytest.mark.parametrize(("header", "start", "end"), [
    ("bytes=0-9", 0, 9),
    ("bytes=100-", 100, len(TINY_MP4) - 1),
    ("bytes=-50", len(TINY_MP4) - 50, len(TINY_MP4) - 1),
    ("bytes=10-999999", 10, len(TINY_MP4) - 1),
])
def test_range_requests(client, video, header, start, end):
    _, path = video
    response = client.get(path, headers={"Range": header})
    assert response.status_code == 206
    assert response.content == TINY_MP4[start:end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(TINY_MP4)}"
    assert response.headers["content-length"] == str(end - start + 1)


def test_unsatisfiable_and_multi_ranges(client, video):
    _, path = video
    beyond = client.get(path, headers={"Range": f"bytes={len(TINY_MP4) + 5}-"})
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{len(TINY_MP4)}"
    multi = client.get(path, headers={"Range": "bytes=0-1,5-6"})
    assert multi.status_code == 200 and multi.content == TINY_MP4
    garbage = client.get(path, headers={"Range": "items=0-1"})
    assert garbage.status_code == 200


def test_etag_revalidation_and_if_range(client, video):
    body, path = video
    etag = client.get(path).headers["etag"]
    not_modified = client.get(path, headers={"If-None-Match": etag})
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    stale_range = client.get(path, headers={"Range": "bytes=0-9", "If-Range": '"other"'})
    assert stale_range.status_code == 200
    fresh_range = client.get(path, headers={"Range": "bytes=0-9", "If-Range": etag})
    assert fresh_range.status_code == 206


def test_head_request(client, video):
    _, path = video
    response = client.head(path)
    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(TINY_MP4))
    assert response.content == b""


def test_download_forces_attachment(client, video):
    body, _ = video
    response = client.get(local_path(body["downloadUrl"]))
    assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize(("filename", "mime", "kind", "disposition", "content_type"), [
    ("shot.png", "image/png", None, "inline", "image/png"),
    ("device.log", "text/plain", None, "inline", "text/plain; charset=utf-8"),
    ("net.har", "application/json", None, "inline", "application/json; charset=utf-8"),
    ("archive.zip", "application/zip", "file", "attachment", "application/zip"),
    ("dom.html", "text/html", None, "attachment", "application/octet-stream"),
    ("page.mhtml", "multipart/related", None, "attachment", "application/octet-stream"),
    ("vector.svg", "image/svg+xml", "file", "attachment", "application/octet-stream"),
    ("report.pdf", "application/pdf", "file", "attachment", "application/pdf"),
])
def test_content_disposition_rules(client, filename, mime, kind, disposition, content_type):
    content = png_bytes() if mime == "image/png" else (b"%PDF-1.4" if mime == "application/pdf" else b"<x>")
    if mime == "application/zip":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.txt", "a")
        content = buffer.getvalue()
    body = upload(client, content, filename, mime, project="acme/web", kind=kind).json()
    response = client.get(local_path(body["rawUrl"]))
    assert response.status_code == 200
    assert response.headers["content-disposition"].split(";")[0] == disposition
    assert response.headers["content-type"] == content_type
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"


def test_unicode_filename_disposition(client):
    body = upload(client, b"x", "Ekran görüntüsü.txt", "text/plain", project="acme/web").json()
    response = client.get(local_path(body["downloadUrl"]))
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''Ekran%20g%C3%B6r%C3%BCnt%C3%BCs%C3%BC.txt" in disposition


def test_unknown_and_malformed_ids(client):
    assert client.get("/raw/01ARZ3NDEKTSV4RRFFQ69G5FAV/x.png").status_code == 404
    assert client.get("/raw/not-an-id/x.png").status_code == 404
    assert client.get("/thumb/not-an-id.jpg").status_code == 404


def test_missing_file_on_disk(client, ctx):
    body = upload(client, b"x", "a.txt", "text/plain", project="acme/web").json()
    (ctx.paths.store_dir / "acme" / "web" / "_project" / body["id"] / "a.txt").unlink()
    response = client.get(local_path(body["rawUrl"]))
    assert response.status_code == 404 and response.json()["error"] == "file_missing"


def test_zip_download_of_selected_artifacts(client):
    first = upload(client, b"one", "a.txt", "text/plain", project="acme/web", session="s1").json()
    second = upload(client, b"two", "a.txt", "text/plain", project="acme/web", session="s1").json()
    third = upload(client, png_bytes(), "p.png", "image/png", project="acme/web", session="s2").json()
    response = client.get("/api/artifacts/zip", params={"ids": f"{first['id']},{second['id']},{third['id']}"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="artifacts.zip"')
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(archive.namelist())
    assert names == ["s1/a (2).txt", "s1/a.txt", "s2/p.png"]
    assert archive.read("s1/a.txt") == b"one"
    assert archive.read("s1/a (2).txt") == b"two"
    assert archive.testzip() is None

    session_zip = client.get("/api/artifacts/zip", params={"project": "acme/web", "session": "s1"})
    session_archive = zipfile.ZipFile(io.BytesIO(session_zip.content))
    assert sorted(session_archive.namelist()) == ["s1/a (2).txt", "s1/a.txt"]
    assert session_zip.headers["content-disposition"].startswith('attachment; filename="acme-web-s1.zip"')

    posted = client.post("/api/artifacts/zip", json={"ids": [third["id"]]})
    assert zipfile.ZipFile(io.BytesIO(posted.content)).namelist() == ["s2/p.png"]

    missing = client.get("/api/artifacts/zip", params={"ids": "01ARZ3NDEKTSV4RRFFQ69G5FAV"})
    assert missing.status_code == 404
    nothing = client.get("/api/artifacts/zip")
    assert nothing.status_code == 400


@pytest.mark.parametrize(("filename", "mime"), [("clip.mp4", "video/mp4"), ("voice.m4a", "audio/mp4")])
def test_inline_media_is_not_sandboxed(client, filename, mime):
    body = upload(client, b"\x00\x00\x00\x18ftypmp42", filename, mime, project="acme/web").json()
    response = client.get(local_path(body["rawUrl"]))
    csp = response.headers["content-security-policy"]
    assert "sandbox" not in csp
    assert "media-src 'self'" in csp and "default-src 'none'" in csp
    download = client.get(local_path(body["downloadUrl"]))
    assert download.headers["content-security-policy"] == "sandbox"
