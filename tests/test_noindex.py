from __future__ import annotations

from fastapi.testclient import TestClient

from eks_harness.auth.middleware import ROBOTS_TAG
from test_store_support import local_path, png_bytes, upload

STRICT = "noindex, nofollow, noarchive, nosnippet, noimageindex"


def test_robots_constant_is_strict() -> None:
    assert ROBOTS_TAG == STRICT


def test_api_json_has_strict_robots_tag(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == STRICT


def test_docs_and_unknown_routes_have_strict_robots_tag(client: TestClient) -> None:
    assert client.get("/api/docs").headers["x-robots-tag"] == STRICT
    assert client.get("/does-not-exist").headers["x-robots-tag"] == STRICT


def test_raw_artifact_hotlinks_carry_strict_robots_tag(client: TestClient) -> None:
    body = upload(client, png_bytes(), "shot.png", "image/png",
                  project="acme/web", session="s", kind="screenshot").json()
    path = local_path(body["rawUrl"])
    plain = client.get(path)
    assert plain.status_code == 200
    assert plain.headers["x-robots-tag"] == STRICT
    hotlinked = client.get(path, headers={"Referer": "https://someone-else.example/p"})
    assert hotlinked.status_code == 200
    assert hotlinked.headers["x-robots-tag"] == STRICT
