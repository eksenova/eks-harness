from __future__ import annotations

import hashlib

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from eks_harness.api.routes_auth import login_limiter
from eks_harness.api.deps import ProjectAccess, SessionAccess, event_filter_for, require_project, require_session
from eks_harness.auth import core as auth_core
from eks_harness.auth import keys as auth_keys
from eks_harness.auth import proxies
from eks_harness.auth.exposure import ExposureError, ensure_safe_bind, exposure_problem
from eks_harness.auth.ratelimit import Limit, LoginRateLimiter, login_keys
from eks_harness.config import Config
from eks_harness.daemon.events import Event
from eks_harness.db.repos import api_keys, users, web_sessions
from conftest import ADMIN_PASSWORD, AuthEnv, bearer, create_user, ensure_session, make_app

CSRF = auth_core.CSRF_HEADER


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def cookie_headers(response) -> list[str]:
    return [value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"]


class TestAuthDisabled:
    def test_every_caller_is_the_local_admin(self, client: TestClient) -> None:
        me = client.get("/api/auth/me").json()
        assert me["via"] == "local"
        assert me["authEnabled"] is False
        assert me["user"]["username"] == "local" and me["user"]["role"] == "admin"

    def test_login_is_refused(self, client: TestClient) -> None:
        response = login(client, "anyone", "whatever-password")
        assert response.status_code == 400
        assert response.json()["error"] == "auth_disabled"

    def test_bearer_is_ignored_and_builtin_cannot_hold_keys(self, client: TestClient) -> None:
        assert client.get("/api/auth/me", headers=bearer("ehk_bad")).status_code == 200
        response = client.post("/api/keys", json={"name": "x"})
        assert response.status_code == 400
        assert response.json()["error"] == "builtin_user"


class TestBearer:
    def test_no_credentials_is_401(self, auth_env: AuthEnv) -> None:
        response = auth_env.client.get("/api/auth/me")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("key", ["garbage", "ehk_abcdefgh_short", "ehk_ABCDEFGH_" + "a" * 32])
    def test_bad_key_is_401(self, auth_env: AuthEnv, key: str) -> None:
        response = auth_env.client.get("/api/auth/me", headers=bearer(key))
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_api_key"

    def test_wrong_secret_for_known_prefix(self, auth_env: AuthEnv) -> None:
        prefix = auth_env.admin_key.split("_")[1]
        forged = f"ehk_{prefix}_{'x' * 32}"
        assert auth_env.client.get("/api/auth/me", headers=bearer(forged)).status_code == 401

    def test_valid_key(self, auth_env: AuthEnv) -> None:
        me = auth_env.client.get("/api/auth/me", headers=auth_env.admin_headers).json()
        assert me["via"] == "key"
        assert me["user"]["username"] == "admin"
        assert me["keyPrefix"] == auth_env.admin_key.split("_")[1]
        assert me["csrfToken"] is None

    def test_bearer_needs_no_csrf(self, auth_env: AuthEnv) -> None:
        response = auth_env.client.post("/api/keys", json={"name": "ci"}, headers=auth_env.admin_headers)
        assert response.status_code == 201

    def test_key_is_stored_as_sha256_only(self, auth_env: AuthEnv) -> None:
        _, prefix, secret = auth_env.admin_key.split("_")
        record = api_keys.get_by_prefix(auth_env.db.conn(), prefix)
        assert record.secret_sha256 == hashlib.sha256(secret.encode()).hexdigest()
        dump = "\n".join(auth_env.db.conn().iterdump())
        assert secret not in dump

    def test_disabled_user_key_is_rejected(self, auth_env: AuthEnv) -> None:
        user, key = auth_env.make_user("mia")
        with auth_env.db.transaction() as conn:
            users.update(conn, user.id, disabled=True)
        assert auth_env.client.get("/api/auth/me", headers=bearer(key)).status_code == 401


class TestCookieLogin:
    def test_password_login_sets_strict_httponly_cookie(self, auth_env: AuthEnv) -> None:
        response = login(auth_env.client, "admin", ADMIN_PASSWORD)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["username"] == "admin" and body["csrfToken"]
        [cookie] = cookie_headers(response)
        assert cookie.startswith(f"{auth_core.COOKIE_NAME}=")
        lowered = cookie.lower()
        assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
        assert "secure" not in lowered.replace("samesite", "")
        assert response.headers["cache-control"] == "no-store"

    def test_cookie_is_secure_with_https_public_url(self, auth_config: Config) -> None:
        auth_config.set("server.publicUrl", "https://share.example.com")
        app = make_app(auth_config)
        create_user(app.state.ctx.db, "admin", "admin", ADMIN_PASSWORD)
        with TestClient(app) as client:
            [cookie] = cookie_headers(login(client, "admin", ADMIN_PASSWORD))
        assert "; secure" in cookie.lower()

    def test_web_session_token_is_hashed(self, auth_env: AuthEnv) -> None:
        response = login(auth_env.client, "admin", ADMIN_PASSWORD)
        token = response.cookies.get(auth_core.COOKIE_NAME)
        conn = auth_env.db.conn()
        assert web_sessions.get(conn, token) is None
        assert web_sessions.get(conn, hashlib.sha256(token.encode()).hexdigest()) is not None

    def test_cookie_authenticates_and_csrf_guards_unsafe_methods(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        csrf = login(client, "admin", ADMIN_PASSWORD).json()["csrfToken"]
        me = client.get("/api/auth/me").json()
        assert me["via"] == "cookie" and me["csrfToken"] == csrf
        refused = client.post("/api/keys", json={"name": "web"})
        assert refused.status_code == 403 and refused.json()["error"] == "csrf_failed"
        wrong = client.post("/api/keys", json={"name": "web"}, headers={CSRF: "nope"})
        assert wrong.status_code == 403
        accepted = client.post("/api/keys", json={"name": "web"}, headers={CSRF: csrf})
        assert accepted.status_code == 201

    def test_api_key_login_gives_a_cookie(self, auth_env: AuthEnv) -> None:
        response = auth_env.client.post("/api/auth/login", json={"apiKey": auth_env.admin_key})
        assert response.status_code == 200
        assert auth_env.client.get("/api/auth/me").json()["via"] == "cookie"

    def test_wrong_password_and_unknown_user(self, auth_env: AuthEnv) -> None:
        wrong = login(auth_env.client, "admin", "not-the-password")
        assert wrong.status_code == 401 and wrong.json()["error"] == "invalid_credentials"
        assert login(auth_env.client, "nobody", "whatever-1").status_code == 401
        assert auth_env.client.post("/api/auth/login", json={"apiKey": "ehk_bad"}).status_code == 401
        assert auth_env.client.post("/api/auth/login", json={"username": "admin"}).status_code == 400

    def test_disabled_user_cannot_log_in(self, auth_env: AuthEnv) -> None:
        create_user(auth_env.db, "sam", "member", "sam-password-1")
        assert login(auth_env.client, "sam", "sam-password-1").status_code == 200
        auth_env.client.cookies.clear()
        auth_env.client.patch("/api/users/sam", json={"disabled": True}, headers=auth_env.admin_headers)
        assert login(auth_env.client, "sam", "sam-password-1").status_code == 401

    def test_logout_ends_the_session_and_needs_csrf(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        csrf = login(client, "admin", ADMIN_PASSWORD).json()["csrfToken"]
        assert client.post("/api/auth/logout").status_code == 403
        token = client.cookies.get(auth_core.COOKIE_NAME)
        response = client.post("/api/auth/logout", headers={CSRF: csrf})
        assert response.status_code == 200
        assert any("max-age=0" in c.lower() for c in cookie_headers(response))
        client.cookies.set(auth_core.COOKIE_NAME, token)
        assert client.get("/api/auth/me").status_code == 401

    def test_expired_session_is_rejected(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        login(client, "admin", ADMIN_PASSWORD)
        with auth_env.db.transaction() as conn:
            conn.execute("UPDATE web_sessions SET expires_at = 1")
        assert client.get("/api/auth/me").status_code == 401

    def test_login_rate_limit_slows_down_but_never_locks_out(self, auth_env: AuthEnv) -> None:
        client = auth_env.client
        clock = [5000.0]
        limiter = login_limiter(auth_env.app.state.ctx)
        limiter.clock = lambda: clock[0]
        for _ in range(8):
            assert login(client, "admin", "wrong-password").status_code in (401, 429)
            clock[0] += 60
        blocked = login(client, "admin", "wrong-password")
        assert blocked.status_code == 401
        refused = login(client, "admin", ADMIN_PASSWORD)
        assert refused.status_code == 429
        assert refused.json()["error"] == "too_many_attempts"
        assert 0 < int(refused.headers["retry-after"]) <= 60
        clock[0] += int(refused.headers["retry-after"])
        assert login(client, "admin", ADMIN_PASSWORD).status_code == 200
        create_user(auth_env.db, "other", "member", "other-password")
        assert login(client, "other", "other-password").status_code == 200


class TestRateLimiter:
    def test_exponential_delay_and_success_reset(self) -> None:
        clock = [1000.0]
        limiter = LoginRateLimiter([Limit("ip:", 3, 60, max_delay=30), Limit("user:", 2, 60, max_delay=30)],
                                   clock=lambda: clock[0])
        keys = login_keys("1.2.3.4", "Ada")
        assert keys == ["ip:1.2.3.4", "user:ada"]
        limiter.failure(keys)
        assert limiter.retry_after(keys) is None
        limiter.failure(keys)
        assert limiter.retry_after(keys) == 1
        limiter.failure(keys)
        assert limiter.retry_after(keys) == 2
        for _ in range(10):
            limiter.failure(keys)
        assert limiter.retry_after(keys) == 30
        clock[0] += 30
        assert limiter.retry_after(keys) is None
        limiter.success(keys)
        limiter.failure(login_keys("1.2.3.4", "bob"))
        assert limiter.retry_after(["user:bob"]) is None
        assert limiter.retry_after(["ip:1.2.3.4"]) == 30
        clock[0] += 61
        assert limiter.retry_after(["ip:1.2.3.4"]) is None


class TestKeys:
    def test_created_key_is_shown_once_and_works(self, auth_env: AuthEnv) -> None:
        user, key = auth_env.make_user("mia")
        created = auth_env.client.post("/api/keys", json={"name": "laptop"}, headers=bearer(key))
        assert created.status_code == 201
        raw = created.json()["key"]
        assert raw.startswith("ehk_") and auth_keys.parse_api_key(raw)
        listed = auth_env.client.get("/api/keys", headers=bearer(key)).json()["items"]
        assert {k["name"] for k in listed} == {"laptop", "mia test key"}
        assert all("key" not in k for k in listed)
        assert auth_env.client.get("/api/auth/me", headers=bearer(raw)).json()["user"]["username"] == "mia"

    def test_member_sees_only_own_keys(self, auth_env: AuthEnv) -> None:
        _, key = auth_env.make_user("mia")
        assert auth_env.client.get("/api/keys", params={"all": "true"}, headers=bearer(key)).status_code == 403
        assert auth_env.client.get("/api/keys", params={"user": "admin"}, headers=bearer(key)).status_code == 403
        created = auth_env.client.post("/api/keys", json={"username": "admin"}, headers=bearer(key))
        assert created.status_code == 403

    def test_admin_lists_all_and_creates_for_others(self, auth_env: AuthEnv) -> None:
        auth_env.make_user("mia")
        every = auth_env.client.get("/api/keys", params={"all": "true"}, headers=auth_env.admin_headers).json()
        assert {k["username"] for k in every["items"]} == {"admin", "mia"}
        created = auth_env.client.post("/api/keys", json={"username": "mia", "name": "ci"},
                                       headers=auth_env.admin_headers)
        assert created.status_code == 201 and created.json()["username"] == "mia"
        mine = auth_env.client.get("/api/keys", params={"user": "mia"}, headers=auth_env.admin_headers).json()
        assert len(mine["items"]) == 2

    def test_revoke_own_and_not_others(self, auth_env: AuthEnv) -> None:
        _, mia_key = auth_env.make_user("mia")
        _, bob_key = auth_env.make_user("bob")
        bob_prefix = bob_key.split("_")[1]
        assert auth_env.client.delete(f"/api/keys/{bob_prefix}", headers=bearer(mia_key)).status_code == 404
        revoked = auth_env.client.delete(f"/api/keys/{bob_key}", headers=bearer(bob_key))
        assert revoked.status_code == 200 and revoked.json()["active"] is False
        assert auth_env.client.get("/api/auth/me", headers=bearer(bob_key)).status_code == 401
        assert auth_env.client.delete(f"/api/keys/{mia_key.split('_')[1]}",
                                      headers=auth_env.admin_headers).status_code == 200
        assert auth_env.client.get("/api/auth/me", headers=bearer(mia_key)).status_code == 401

    def test_active_filter(self, auth_env: AuthEnv) -> None:
        extra = auth_env.client.post("/api/keys", json={"name": "old"}, headers=auth_env.admin_headers).json()
        auth_env.client.delete(f"/api/keys/{extra['id']}", headers=auth_env.admin_headers)
        active = auth_env.client.get("/api/keys", params={"includeRevoked": "false"},
                                     headers=auth_env.admin_headers).json()["items"]
        assert [k["name"] for k in active] == ["admin test key"]


class TestUsers:
    def test_members_cannot_manage_users(self, auth_env: AuthEnv) -> None:
        _, key = auth_env.make_user("mia")
        assert auth_env.client.get("/api/users", headers=bearer(key)).status_code == 403
        response = auth_env.client.post("/api/users", json={"username": "x"}, headers=bearer(key))
        assert response.status_code == 403 and response.json()["error"] == "admin_required"

    def test_crud(self, auth_env: AuthEnv) -> None:
        client, headers = auth_env.client, auth_env.admin_headers
        created = client.post("/api/users", json={"username": "ada", "password": "ada-password", "role": "member"},
                              headers=headers)
        assert created.status_code == 201 and created.json()["hasPassword"] is True
        assert client.post("/api/users", json={"username": "ADA"}, headers=headers).status_code == 409
        assert client.post("/api/users", json={"username": "short", "password": "x"}, headers=headers).status_code == 422
        assert client.post("/api/users", json={"username": "local"}, headers=headers).status_code == 400
        names = [u["username"] for u in client.get("/api/users", headers=headers).json()["items"]]
        assert names == ["ada", "admin"]
        with_builtin = client.get("/api/users", params={"includeBuiltin": "true"}, headers=headers).json()["items"]
        assert "local" in [u["username"] for u in with_builtin]
        renamed = client.patch("/api/users/ada", json={"username": "ada2", "role": "admin"}, headers=headers)
        assert renamed.status_code == 200 and renamed.json()["role"] == "admin"
        by_id = client.get(f"/api/users/{renamed.json()['id']}", headers=headers)
        assert by_id.json()["username"] == "ada2"
        assert client.delete("/api/users/ada2", headers=headers).status_code == 200
        assert client.get("/api/users/ada2", headers=headers).status_code == 404

    def test_builtin_user_is_protected(self, auth_env: AuthEnv) -> None:
        response = auth_env.client.patch("/api/users/local", json={"role": "member"}, headers=auth_env.admin_headers)
        assert response.status_code == 403
        assert auth_env.client.delete("/api/users/local", headers=auth_env.admin_headers).status_code == 403

    def test_last_admin_is_protected(self, auth_env: AuthEnv) -> None:
        client, headers = auth_env.client, auth_env.admin_headers
        for body in ({"role": "member"}, {"disabled": True}):
            response = client.patch("/api/users/admin", json=body, headers=headers)
            assert response.status_code == 409 and response.json()["error"] == "last_admin"
        assert client.delete("/api/users/admin", headers=headers).status_code == 409
        client.post("/api/users", json={"username": "second", "role": "admin"}, headers=headers)
        assert client.patch("/api/users/admin", json={"role": "member"}, headers=headers).status_code == 200

    def test_password_change_ends_other_sessions_and_disable_ends_all(self, auth_env: AuthEnv) -> None:
        create_user(auth_env.db, "sam", "member", "sam-password-1")
        sam = TestClient(auth_env.app)
        assert login(sam, "sam", "sam-password-1").status_code == 200
        assert sam.get("/api/auth/me").status_code == 200
        auth_env.client.patch("/api/users/sam", json={"password": "sam-password-2"}, headers=auth_env.admin_headers)
        assert sam.get("/api/auth/me").status_code == 401
        assert login(sam, "sam", "sam-password-1").status_code == 401
        assert login(sam, "sam", "sam-password-2").status_code == 200
        auth_env.client.patch("/api/users/sam", json={"disabled": True}, headers=auth_env.admin_headers)
        assert sam.get("/api/auth/me").status_code == 401

    def test_deleting_a_user_removes_keys_and_grants(self, auth_env: AuthEnv) -> None:
        user, key = auth_env.make_user("mia")
        auth_env.grant(user, "acme/web", "viewer")
        auth_env.client.delete("/api/users/mia", headers=auth_env.admin_headers)
        conn = auth_env.db.conn()
        assert api_keys.list_keys(conn, user.id) == []
        assert conn.execute("SELECT COUNT(*) FROM grants WHERE user_id = ?", (user.id,)).fetchone()[0] == 0


class TestGrantsApi:
    def test_crud(self, auth_env: AuthEnv) -> None:
        client, headers = auth_env.client, auth_env.admin_headers
        user, _ = auth_env.make_user("mia")
        ensure_session(auth_env.db, "acme/web", "feature/login")
        project = client.post("/api/grants", json={"username": "mia", "project": "acme/web", "level": "editor"},
                              headers=headers)
        assert project.status_code == 201 and project.json()["sessionId"] is None
        session = client.post("/api/grants", json={"userId": user.id, "project": "acme/web",
                                                    "session": "feature/login"}, headers=headers)
        assert session.status_code == 201 and session.json()["sessionSlug"] == "feature-login"
        again = client.post("/api/grants", json={"username": "mia", "project": "acme/web", "level": "viewer"},
                            headers=headers)
        assert again.json()["id"] == project.json()["id"] and again.json()["level"] == "viewer"
        listed = client.get("/api/grants", params={"user": "mia"}, headers=headers).json()["items"]
        assert len(listed) == 2
        only_session = client.get("/api/grants", params={"project": "acme/web", "session": "feature-login"},
                                  headers=headers).json()["items"]
        assert [g["id"] for g in only_session] == [session.json()["id"]]
        assert client.delete(f"/api/grants/{session.json()['id']}", headers=headers).status_code == 200
        assert client.delete(f"/api/grants/{session.json()['id']}", headers=headers).status_code == 404

    def test_validation(self, auth_env: AuthEnv) -> None:
        client, headers = auth_env.client, auth_env.admin_headers
        auth_env.make_user("mia")
        assert client.post("/api/grants", json={"username": "mia", "project": "Bad Id"}, headers=headers).status_code == 400
        missing_session = client.post("/api/grants", json={"username": "mia", "project": "acme/web", "session": "x"},
                                      headers=headers)
        assert missing_session.status_code == 404
        assert client.post("/api/grants", json={"username": "admin", "project": "acme/web"},
                           headers=headers).status_code == 400
        assert client.post("/api/grants", json={"username": "ghost", "project": "acme/web"},
                           headers=headers).status_code == 404
        created = client.post("/api/grants", json={"username": "mia", "project": "new/project"}, headers=headers)
        assert created.status_code == 201

    def test_members_cannot_manage_grants(self, auth_env: AuthEnv) -> None:
        _, key = auth_env.make_user("mia")
        assert auth_env.client.get("/api/grants", headers=bearer(key)).status_code == 403
        assert auth_env.client.post("/api/grants", json={"username": "mia", "project": "a/b"},
                                    headers=bearer(key)).status_code == 403

    def test_me_lists_own_grants(self, auth_env: AuthEnv) -> None:
        user, key = auth_env.make_user("mia")
        auth_env.grant(user, "acme/web", "editor")
        me = auth_env.client.get("/api/auth/me", headers=bearer(key)).json()
        assert [(g["projectId"], g["level"]) for g in me["grants"]] == [("acme/web", "editor")]


def add_probe_routes(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/api/_probe/p/{owner}/{name}/viewer")
    def project_viewer(access: ProjectAccess = Depends(require_project("viewer"))) -> dict:
        return {"level": access.level, "limited": access.limited}

    @router.get("/api/_probe/p/{owner}/{name}/editor")
    def project_editor(access: ProjectAccess = Depends(require_project("editor"))) -> dict:
        return {"level": access.level, "limited": access.limited}

    @router.get("/api/_probe/p/{owner}/{name}/s/{slug}/viewer")
    def session_viewer(access: SessionAccess = Depends(require_session("viewer"))) -> dict:
        return {"level": access.level}

    @router.get("/api/_probe/p/{owner}/{name}/s/{slug}/editor")
    def session_editor(access: SessionAccess = Depends(require_session("editor"))) -> dict:
        return {"level": access.level}

    app.router.routes[:0] = router.routes


GRANT_MATRIX = [
    (None, None, {"pv": 404, "pe": 404, "s1v": 404, "s1e": 404, "s2v": 404, "s2e": 404}),
    ("viewer", None, {"pv": 200, "pe": 403, "s1v": 200, "s1e": 403, "s2v": 200, "s2e": 403}),
    ("editor", None, {"pv": 200, "pe": 200, "s1v": 200, "s1e": 200, "s2v": 200, "s2e": 200}),
    ("viewer", "s1", {"pv": 200, "pe": 403, "s1v": 200, "s1e": 403, "s2v": 404, "s2e": 404}),
    ("editor", "s1", {"pv": 200, "pe": 403, "s1v": 200, "s1e": 200, "s2v": 404, "s2e": 404}),
    ("admin", None, {"pv": 200, "pe": 200, "s1v": 200, "s1e": 200, "s2v": 200, "s2e": 200}),
]


@pytest.mark.parametrize("level,session,expected", GRANT_MATRIX,
                         ids=["none", "project-viewer", "project-editor", "session-viewer", "session-editor", "admin"])
def test_grant_matrix(auth_env: AuthEnv, level: str | None, session: str | None, expected: dict) -> None:
    add_probe_routes(auth_env.app)
    ensure_session(auth_env.db, "acme/web", "s1")
    ensure_session(auth_env.db, "acme/web", "s2")
    if level == "admin":
        headers = auth_env.admin_headers
    else:
        user, key = auth_env.make_user("mia")
        headers = bearer(key)
        if level:
            auth_env.grant(user, "acme/web", level, session)
    base = "/api/_probe/p/acme/web"
    paths = {"pv": f"{base}/viewer", "pe": f"{base}/editor", "s1v": f"{base}/s/s1/viewer",
             "s1e": f"{base}/s/s1/editor", "s2v": f"{base}/s/s2/viewer", "s2e": f"{base}/s/s2/editor"}
    got = {name: auth_env.client.get(path, headers=headers).status_code for name, path in paths.items()}
    assert got == expected
    if level == "viewer" and session == "s1":
        assert auth_env.client.get(paths["pv"], headers=headers).json() == {"level": "viewer", "limited": True}
    if level == "editor" and session is None:
        assert auth_env.client.get(paths["pv"], headers=headers).json() == {"level": "editor", "limited": False}


def test_grants_elsewhere_do_not_leak(auth_env: AuthEnv) -> None:
    add_probe_routes(auth_env.app)
    ensure_session(auth_env.db, "acme/web", "s1")
    user, key = auth_env.make_user("mia")
    auth_env.grant(user, "other/app", "editor")
    response = auth_env.client.get("/api/_probe/p/acme/web/viewer", headers=bearer(key))
    assert response.status_code == 404 and response.json()["error"] == "project_not_found"


def test_event_filter_follows_grants(auth_env: AuthEnv) -> None:
    user, _ = auth_env.make_user("mia")
    s1 = ensure_session(auth_env.db, "acme/web", "s1")
    s2 = ensure_session(auth_env.db, "acme/web", "s2")
    auth_env.grant(user, "acme/web", "viewer", "s1")
    from eks_harness.auth.core import Principal
    member = Principal(user_id=user.id, username="mia", role="member", via="key")
    accept = event_filter_for(auth_env.db, member)

    def event(**fields) -> Event:
        return Event(id=1, ts=0.0, type="artifact.created", **fields)

    assert accept(event(session_id=s1.id, project_id="acme/web"))
    assert not accept(event(session_id=s2.id, project_id="acme/web"))
    assert not accept(event(project_id="acme/web"))
    assert accept(event(resource="ios:1"))
    admin = Principal(user_id=auth_env.admin.id, username="admin", role="admin", via="key")
    assert event_filter_for(auth_env.db, admin)(event(project_id="other/x"))


def _request(config: Config, peer: str, headers: dict[str, str], scheme: str = "http") -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "scheme": scheme, "server": ("testserver", 80),
             "client": (peer, 5000), "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope)


class TestClientIp:
    def test_untrusted_peer_headers_are_ignored(self, config: Config) -> None:
        request = _request(config, "203.0.113.9", {"X-Forwarded-For": "1.1.1.1", "CF-Connecting-IP": "2.2.2.2",
                                                   "X-Forwarded-Proto": "https"})
        assert proxies.client_ip(request, config) == "203.0.113.9"
        assert proxies.client_scheme(request, config) == "http"

    def test_trusted_proxy_chain(self, config: Config) -> None:
        config.set("server.trustedProxies", ["10.0.0.0/8", "127.0.0.1/32"])
        request = _request(config, "127.0.0.1", {"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 10.1.2.3",
                                                 "X-Forwarded-Proto": "https"})
        assert proxies.client_ip(request, config) == "198.51.100.7"
        assert proxies.client_scheme(request, config) == "https"

    def test_all_hops_trusted_uses_the_peer(self, config: Config) -> None:
        config.set("server.trustedProxies", ["10.0.0.0/8"])
        request = _request(config, "10.0.0.1", {"X-Forwarded-For": "10.9.9.9, 10.0.0.2"})
        assert proxies.client_ip(request, config) == "10.0.0.1"

    def test_garbage_hop_falls_back_to_peer(self, config: Config) -> None:
        config.set("server.trustedProxies", ["10.0.0.0/8"])
        request = _request(config, "10.0.0.1", {"X-Forwarded-For": "not-an-ip"})
        assert proxies.client_ip(request, config) == "10.0.0.1"

    def test_cloudflare(self, config: Config) -> None:
        config.set("server.trustedProxies", ["127.0.0.1/32"])
        headers = {"CF-Connecting-IP": "2001:db8::5", "X-Forwarded-For": "9.9.9.9"}
        assert proxies.client_ip(_request(config, "127.0.0.1", headers), config) == "9.9.9.9"
        config.set("server.cloudflare", True)
        assert proxies.client_ip(_request(config, "127.0.0.1", headers), config) == "2001:db8::5"
        assert proxies.client_ip(_request(config, "127.0.0.2", headers), config) == "127.0.0.2"

    def test_login_records_resolved_ip(self, auth_env: AuthEnv) -> None:
        ctx = auth_env.app.state.ctx
        ctx.config.set("server.trustedProxies", ["127.0.0.1/32"])
        proxied = TestClient(auth_env.app, client=("127.0.0.1", 50000))
        response = proxied.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD},
                                        headers={"X-Forwarded-For": "198.51.100.23"})
        assert response.status_code == 200
        [record] = web_sessions.list_for_user(auth_env.db.conn(), auth_env.admin.id)
        assert record.ip == "198.51.100.23"


class TestExposure:
    @pytest.mark.parametrize("host,auth,refused", [
        ("127.0.0.1", False, False), ("localhost", False, False), ("::1", False, False),
        ("0.0.0.0", False, True), ("192.168.1.20", False, True), ("0.0.0.0", True, False),
    ])
    def test_problem(self, host: str, auth: bool, refused: bool) -> None:
        assert (exposure_problem(host, auth) is not None) is refused

    def test_ensure_safe_bind(self, config: Config) -> None:
        ensure_safe_bind(config)
        config.set("server.host", "0.0.0.0")
        with pytest.raises(ExposureError):
            ensure_safe_bind(config)
        ensure_safe_bind(config, force=True)
        config.set("auth.enabled", True)
        ensure_safe_bind(config)


def test_password_hashing_is_argon2id() -> None:
    hashed = auth_core.hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert auth_core.verify_password(hashed, "correct horse battery")
    assert not auth_core.verify_password(hashed, "wrong")
    assert not auth_core.verify_password(None, "x")
    assert not auth_core.verify_password("not-a-hash", "x")
