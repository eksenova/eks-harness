from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from eks_harness.config import DEFAULTS, SETTINGS, env_name
from eks_harness.db.repos import events, settings_audit

from conftest import AuthEnv, bearer, create_key, create_user, make_app


def rows_by_key(payload: dict) -> dict[str, dict]:
    return {row["key"]: row for row in payload["settings"]}


def test_get_lists_every_setting_with_metadata(client: TestClient) -> None:
    payload = client.get("/api/settings").json()
    rows = rows_by_key(payload)
    assert set(rows) == set(SETTINGS)
    port = rows["server.port"]
    assert port["value"] == 7171 and port["default"] == 7171
    assert port["restartRequired"] is True and port["group"] == "server" and port["groupTitle"] == "Server"
    assert port["description"] and port["envName"] == "EKS_HARNESS_SERVER_PORT" and port["source"] == "default"
    assert rows["storage.quotaGb"]["nullable"] is True
    assert payload["restartPending"] is False and payload["configFile"].endswith("config.json")


def test_settings_are_admin_only(auth_env: AuthEnv) -> None:
    _, key = auth_env.make_user("mia")
    assert auth_env.client.get("/api/settings", headers=bearer(key)).status_code == 403
    assert auth_env.client.patch("/api/settings", json={"values": {"live.maxFps": 5}},
                                 headers=bearer(key)).status_code == 403
    assert auth_env.client.get("/api/settings").status_code == 401
    assert auth_env.client.get("/api/settings", headers=auth_env.admin_headers).status_code == 200


def test_patch_applies_audits_and_flags_restart(client: TestClient, ctx) -> None:
    response = client.patch("/api/settings", json={"values": {"server.port": "7272", "live.maxFps": 5,
                                                              "server.trustedProxies": "10.0.0.0/8, 127.0.0.1/32"}})
    assert response.status_code == 200, response.text
    body = response.json()
    changed = {c["key"]: c for c in body["changed"]}
    assert changed["server.port"] == {"key": "server.port", "old": 7171, "new": 7272, "restartRequired": True}
    assert changed["live.maxFps"]["restartRequired"] is False
    assert changed["server.trustedProxies"]["new"] == ["10.0.0.0/8", "127.0.0.1/32"]
    assert body["restartRequired"] is True
    assert body["settings"]["restartPendingKeys"] == ["server.port"]
    rows = rows_by_key(body["settings"])
    assert rows["server.port"]["pendingRestart"] is True and rows["server.port"]["source"] == "file"
    assert ctx.config["live.maxFps"] == 5
    stored = json.loads(ctx.config.file.read_text())
    assert stored["server.port"] == 7272
    audit = settings_audit.list_entries(ctx.db.conn())
    by_key = {e.key: (e.old, e.new, e.user) for e in audit}
    assert by_key["server.port"] == (7171, 7272, "local")
    assert by_key["live.maxFps"] == (10, 5, "local")
    assert by_key["server.trustedProxies"] == ([], ["10.0.0.0/8", "127.0.0.1/32"], "local")
    listed = client.get("/api/settings/audit", params={"key": "server.port"}).json()
    assert [(e["key"], e["old"], e["new"]) for e in listed] == [("server.port", 7171, 7272)]
    [event] = [e for e in events.list_events(ctx.db.conn()) if e.type == "settings.changed"]
    assert sorted(event.detail["keys"]) == ["live.maxFps", "server.port", "server.trustedProxies"]
    assert event.detail["restartRequired"] == ["server.port"]


def test_patch_without_change_audits_nothing(client: TestClient, ctx) -> None:
    body = client.patch("/api/settings", json={"values": {"live.maxFps": DEFAULTS["live.maxFps"]}}).json()
    assert body["changed"] == [] and body["restartRequired"] is False
    assert settings_audit.list_entries(ctx.db.conn()) == []


def test_unset_restores_default(client: TestClient, ctx) -> None:
    client.patch("/api/settings", json={"values": {"storage.quotaGb": 50}})
    assert ctx.config["storage.quotaGb"] == 50
    body = client.patch("/api/settings", json={"unset": ["storage.quotaGb"]}).json()
    assert body["changed"] == [{"key": "storage.quotaGb", "old": 50, "new": None, "restartRequired": False}]
    assert "storage.quotaGb" not in json.loads(ctx.config.file.read_text())


@pytest.mark.parametrize("values,field", [
    ({"server.port": 0}, "server.port"),
    ({"server.port": "abc"}, "server.port"),
    ({"server.host": "not-an-ip"}, "server.host"),
    ({"server.publicUrl": "ftp://x"}, "server.publicUrl"),
    ({"server.trustedProxies": ["nonsense"]}, "server.trustedProxies"),
    ({"auth.enabled": "maybe"}, "auth.enabled"),
    ({"no.such.key": 1}, "no.such.key"),
    ({"live.jpegQuality": 101}, "live.jpegQuality"),
])
def test_patch_validation(client: TestClient, ctx, values: dict, field: str) -> None:
    before = ctx.config.as_dict()
    response = client.patch("/api/settings", json={"values": values})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_failed"
    assert [p["field"] for p in body["problems"]] == [field]
    assert ctx.config.as_dict() == before


def test_patch_is_all_or_nothing(client: TestClient, ctx) -> None:
    response = client.patch("/api/settings", json={"values": {"live.maxFps": 5, "server.port": -1}})
    assert response.status_code == 422
    assert ctx.config["live.maxFps"] == DEFAULTS["live.maxFps"]


def test_port_range_order_is_checked(client: TestClient) -> None:
    response = client.patch("/api/settings", json={"values": {"backend.portRangeStart": 6000,
                                                              "backend.portRangeEnd": 5500}})
    assert response.status_code == 422


def test_unknown_unset_and_overlap(client: TestClient) -> None:
    assert client.patch("/api/settings", json={"unset": ["nope"]}).status_code == 422
    overlap = client.patch("/api/settings", json={"values": {"live.maxFps": 3}, "unset": ["live.maxFps"]})
    assert overlap.status_code == 422 and overlap.json()["problems"][0]["type"] == "conflict"


def test_env_overridden_key_is_refused(monkeypatch: pytest.MonkeyPatch, config) -> None:
    monkeypatch.setenv(env_name("live.maxFps"), "7")
    config.reload()
    with TestClient(make_app(config)) as client:
        rows = rows_by_key(client.get("/api/settings").json())
        assert rows["live.maxFps"]["source"] == "env" and rows["live.maxFps"]["value"] == 7
        response = client.patch("/api/settings", json={"values": {"live.maxFps": 3}})
    assert response.status_code == 422
    assert response.json()["problems"][0]["type"] == "env_override"


def test_exposure_is_refused_without_force(client: TestClient, ctx) -> None:
    refused = client.patch("/api/settings", json={"values": {"server.host": "0.0.0.0"}})
    assert refused.status_code == 409 and refused.json()["error"] == "exposure_refused"
    assert ctx.config["server.host"] == "127.0.0.1"
    forced = client.patch("/api/settings", params={"force": "true"}, json={"values": {"server.host": "0.0.0.0"}})
    assert forced.status_code == 200
    assert ctx.config["server.host"] == "0.0.0.0"


def test_exposure_is_allowed_with_auth(client: TestClient, ctx) -> None:
    user = create_user(ctx.db, "root", "admin", "root-password")
    create_key(ctx.db, user.id)
    response = client.patch("/api/settings", json={"values": {"server.host": "0.0.0.0", "auth.enabled": True}})
    assert response.status_code == 200, response.text
    assert ctx.config["auth.enabled"] is True
    assert client.get("/api/settings").status_code == 401


def test_enabling_auth_needs_a_usable_admin(client: TestClient, ctx) -> None:
    refused = client.patch("/api/settings", json={"values": {"auth.enabled": True}})
    assert refused.status_code == 409 and refused.json()["error"] == "no_admin"
    create_user(ctx.db, "keyless", "admin")
    assert client.patch("/api/settings", json={"values": {"auth.enabled": True}}).status_code == 409
    forced = client.patch("/api/settings", params={"force": "true"}, json={"values": {"auth.enabled": True}})
    assert forced.status_code == 200


def test_disabling_auth_on_a_network_bind_is_refused(auth_env: AuthEnv) -> None:
    ctx = auth_env.app.state.ctx
    ctx.config.set("server.host", "0.0.0.0")
    response = auth_env.client.patch("/api/settings", json={"values": {"auth.enabled": False}},
                                     headers=auth_env.admin_headers)
    assert response.status_code == 409
    assert ctx.config["auth.enabled"] is True
    audit = auth_env.client.patch("/api/settings", json={"values": {"live.maxFps": 4}}, headers=auth_env.admin_headers)
    assert audit.status_code == 200
    assert settings_audit.list_entries(auth_env.db.conn(), key="live.maxFps")[0].user == "admin"


def test_settings_patch_by_cookie_needs_csrf(auth_env: AuthEnv) -> None:
    client = auth_env.client
    csrf = client.post("/api/auth/login", json={"username": "admin", "password": auth_env.admin_password}).json()[
        "csrfToken"]
    assert client.patch("/api/settings", json={"values": {"live.maxFps": 4}}).status_code == 403
    assert client.patch("/api/settings", json={"values": {"live.maxFps": 4}},
                        headers={"X-CSRF-Token": csrf}).status_code == 200


def test_every_setting_has_a_label_and_group_title():
    from eks_harness.config import _SETTINGS, GROUPS, LABELS

    keys = {setting.key for setting in _SETTINGS}
    assert sorted(keys - set(LABELS)) == []
    assert sorted(set(LABELS) - keys) == []
    assert sorted({setting.group for setting in _SETTINGS} - set(GROUPS)) == []
