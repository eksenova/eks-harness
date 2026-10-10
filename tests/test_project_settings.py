from __future__ import annotations

from eks_harness import project_settings


def _create(client) -> None:
    assert client.post("/api/projects", json={"id": "acme/web"}).status_code == 201


def test_project_settings_default_to_empty_lists(client) -> None:
    _create(client)
    body = client.get("/api/projects/acme/web").json()
    assert body["settings"] == {"capture.hideSelectors": [], "apps.iosBundleIds": [], "apps.androidPackages": [],
                                "devices.strayProcessPatterns": []}
    described = client.get("/api/projects/acme/web/settings").json()["items"]
    assert [item["key"] for item in described] == list(project_settings.SETTINGS)
    assert all(item["label"] and item["description"] and item["groupTitle"] for item in described)


def test_patch_merges_cleans_and_unsets_project_settings(client) -> None:
    _create(client)
    body = client.patch("/api/projects/acme/web", json={"settings": {
        "apps.iosBundleIds": ["com.acme.app", " com.acme.clip ", "com.acme.app", ""],
        "capture.hideSelectors": ".devtools\n#build-watcher"}}).json()
    assert body["settings"]["apps.iosBundleIds"] == ["com.acme.app", "com.acme.clip"]
    assert body["settings"]["capture.hideSelectors"] == [".devtools", "#build-watcher"]
    body = client.patch("/api/projects/acme/web", json={"settings": {"apps.androidPackages": ["com.acme.app"]}}).json()
    assert body["settings"]["apps.iosBundleIds"] == ["com.acme.app", "com.acme.clip"]
    assert body["settings"]["apps.androidPackages"] == ["com.acme.app"]
    body = client.patch("/api/projects/acme/web", json={"settings": {"apps.iosBundleIds": None}}).json()
    assert body["settings"]["apps.iosBundleIds"] == []
    described = {item["key"]: item for item in client.get("/api/projects/acme/web/settings").json()["items"]}
    assert described["apps.androidPackages"]["isSet"] and not described["apps.iosBundleIds"]["isSet"]


def test_unknown_or_malformed_project_settings_are_refused(client) -> None:
    _create(client)
    unknown = client.patch("/api/projects/acme/web", json={"settings": {"apps.iosBundleId": "com.acme.app"}})
    assert unknown.status_code == 400 and unknown.json()["error"] == "invalid_project_setting"
    malformed = client.patch("/api/projects/acme/web", json={"settings": {"apps.iosBundleIds": {"a": 1}}})
    assert malformed.status_code == 400


def test_stray_patterns_are_the_union_of_every_project(client, ctx) -> None:
    _create(client)
    assert client.post("/api/projects", json={"id": "acme/mobile"}).status_code == 201
    client.patch("/api/projects/acme/web", json={"settings": {"devices.strayProcessPatterns": ["web-server.mjs"]}})
    client.patch("/api/projects/acme/mobile", json={"settings": {"devices.strayProcessPatterns": ["metro", "web-server.mjs"]}})
    assert sorted(project_settings.union(ctx.db.conn(), "devices.strayProcessPatterns")) == ["metro", "web-server.mjs"]
    assert project_settings.for_project(ctx.db.conn(), "acme/web")["devices.strayProcessPatterns"] == ["web-server.mjs"]
