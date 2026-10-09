from __future__ import annotations

import io
import json
import os
import stat
import sys
import types
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from eks_harness.auth import core as auth_core
from eks_harness.auth import keys as auth_keys
from eks_harness.auth import proxies
from eks_harness.cli import credentials as creds
from eks_harness.cli import main, setup_cmd
from eks_harness.cli.client import HarnessClient
from eks_harness.cli.ui import (
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_UNAUTHORIZED,
    EXIT_USAGE,
)
from eks_harness.config import Config
from eks_harness.db import open_database
from eks_harness.db.repos import api_keys, users
from eks_harness.paths import Paths

from conftest import AuthEnv, create_user, ensure_session, make_app


HOP_HEADERS = {"content-length", "transfer-encoding", "connection", "host"}


class AppTransport(httpx.BaseTransport):
    __test__ = False

    def __init__(self, test_client: TestClient) -> None:
        self.test_client = TestClient(test_client.app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in HOP_HEADERS]
        self.test_client.cookies.clear()
        answer = self.test_client.request(request.method, str(request.url), headers=headers, content=body or None)
        reply = [(k, v) for k, v in answer.headers.multi_items() if k.lower() not in HOP_HEADERS]
        return httpx.Response(answer.status_code, headers=reply, content=answer.content, request=request)


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch) -> Callable[[TestClient], None]:
    def apply(test_client: TestClient) -> None:
        original = HarnessClient.__init__
        transport = AppTransport(test_client)

        def init(self, base_url=None, api_key=None, **kwargs):
            kwargs["transport"] = transport
            original(self, "http://testserver", api_key, **kwargs)

        monkeypatch.setattr(HarnessClient, "__init__", init)

    return apply


@pytest.fixture
def stdin(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def feed(text: str) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    return feed


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, *argv: str):
    code, out, err = run(capsys, *argv, "--json")
    assert code == EXIT_OK, err + out
    return json.loads(out)


class TestLogin:
    def test_login_saves_0600_credentials_and_whoami(self, auth_env: AuthEnv, route, stdin, capsys,
                                                      paths: Paths) -> None:
        route(auth_env.client)
        stdin(auth_env.admin_key + "\n")
        result = run_json(capsys, "login", "--key-stdin")
        assert result["username"] == "admin" and result["keyPrefix"] == auth_env.admin_key.split("_")[1]
        mode = stat.S_IMODE(os.stat(paths.credentials_file).st_mode)
        assert mode == 0o600
        assert creds.load(paths).api_key == auth_env.admin_key
        me = run_json(capsys, "whoami")
        assert me["user"]["username"] == "admin" and me["via"] == "key" and me["credentialsSource"] == "file"
        code, out, _ = run(capsys, "whoami")
        assert code == EXIT_OK and "admin" in out

    def test_login_rejects_bad_keys(self, auth_env: AuthEnv, route, stdin, capsys, paths: Paths) -> None:
        route(auth_env.client)
        stdin("not-a-key\n")
        assert run(capsys, "login", "--key-stdin")[0] == EXIT_USAGE
        stdin(f"ehk_AAAAAAAA_{'b' * 32}\n")
        assert run(capsys, "login", "--key-stdin")[0] == EXIT_UNAUTHORIZED
        assert creds.load(paths) is None

    def test_login_needs_a_terminal_or_stdin(self, auth_env: AuthEnv, route, capsys) -> None:
        route(auth_env.client)
        assert run(capsys, "login")[0] == 10

    def test_login_refused_when_auth_is_off(self, client: TestClient, route, stdin, capsys, paths: Paths) -> None:
        route(client)
        stdin(f"ehk_AAAAAAAA_{'b' * 32}\n")
        code, _, err = run(capsys, "login", "--key-stdin")
        assert code == EXIT_ERROR and "disabled" in err
        assert creds.load(paths) is None

    def test_whoami_without_credentials(self, auth_env: AuthEnv, route, capsys) -> None:
        route(auth_env.client)
        assert run(capsys, "whoami")[0] == 10

    def test_logout_revoke(self, auth_env: AuthEnv, route, stdin, capsys, paths: Paths) -> None:
        route(auth_env.client)
        raw, _ = auth_keys.create_api_key(auth_env.db, auth_env.admin.id, "cli")
        stdin(raw + "\n")
        run_json(capsys, "login", "--key-stdin")
        code, _, _ = run(capsys, "logout", "--revoke")
        assert code == EXIT_OK
        assert creds.load(paths) is None
        assert auth_env.client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


class TestKeysUsersGrants:
    @pytest.fixture(autouse=True)
    def as_admin(self, auth_env: AuthEnv, route, monkeypatch: pytest.MonkeyPatch) -> None:
        route(auth_env.client)
        monkeypatch.setenv(creds.API_KEY_ENV, auth_env.admin_key)

    def test_keys(self, auth_env: AuthEnv, capsys) -> None:
        created = run_json(capsys, "keys", "create", "--name", "ci")
        assert created["key"].startswith("ehk_") and created["name"] == "ci"
        code, out, _ = run(capsys, "keys", "create", "--name", "human")
        assert code == EXIT_OK and "ehk_" in out
        listed = run_json(capsys, "keys", "list")
        assert {k["name"] for k in listed["items"]} == {"admin test key", "ci", "human"}
        revoked = run_json(capsys, "keys", "revoke", created["prefix"], "--yes")
        assert revoked["active"] is False
        active = run_json(capsys, "keys", "list", "--active")
        assert "ci" not in {k["name"] for k in active["items"]}
        assert run(capsys, "keys", "revoke", "ZZZZZZZZ", "--yes")[0] == EXIT_NOT_FOUND
        assert run(capsys, "keys", "revoke", created["prefix"])[0] == EXIT_USAGE

    def test_users(self, auth_env: AuthEnv, stdin, capsys) -> None:
        stdin("ada-password\n")
        added = run_json(capsys, "users", "add", "ada", "--password-stdin", "--key")
        assert added["user"]["hasPassword"] is True and added["key"]["key"].startswith("ehk_")
        assert run(capsys, "users", "add", "bob")[0] == EXIT_USAGE
        assert run_json(capsys, "users", "add", "bob", "--no-password")["user"]["hasPassword"] is False
        names = [u["username"] for u in run_json(capsys, "users", "list")["items"]]
        assert names == ["ada", "admin", "bob"]
        edited = run_json(capsys, "users", "edit", "ada", "--role", "admin", "--rename", "ada2")
        assert edited["username"] == "ada2" and edited["role"] == "admin"
        assert run_json(capsys, "users", "disable", "bob")["disabled"] is True
        assert run_json(capsys, "users", "enable", "bob")["disabled"] is False
        assert run(capsys, "users", "edit", "bob")[0] == EXIT_USAGE
        code, _, _ = run(capsys, "users", "delete", "bob", "--yes")
        assert code == EXIT_OK
        assert users.get_by_username(auth_env.db.conn(), "bob") is None
        code, _, err = run(capsys, "users", "delete", "ghost", "--yes")
        assert code == EXIT_NOT_FOUND and "ghost" in err

    def test_member_is_forbidden(self, auth_env: AuthEnv, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
        _, key = auth_env.make_user("mia")
        monkeypatch.setenv(creds.API_KEY_ENV, key)
        code, out, _ = run(capsys, "users", "list", "--json")
        assert code == EXIT_FORBIDDEN
        assert json.loads(out)["error"] == "admin_required"

    def test_grants(self, auth_env: AuthEnv, capsys) -> None:
        auth_env.make_user("mia")
        ensure_session(auth_env.db, "acme/web", "feature/x")
        project = run_json(capsys, "grants", "add", "--user", "mia", "--project", "acme/web", "--level", "editor")
        session = run_json(capsys, "grants", "add", "--user", "mia", "--project", "acme/web", "--session",
                           "feature/x")
        assert project["level"] == "editor" and session["sessionSlug"] == "feature-x"
        assert len(run_json(capsys, "grants", "list", "--user", "mia")["items"]) == 2
        code, _, _ = run(capsys, "grants", "remove", "--user", "mia", "--project", "acme/web", "--session",
                         "feature/x", "--yes")
        assert code == EXIT_OK
        remaining = run_json(capsys, "grants", "list", "--project", "acme/web")["items"]
        assert [g["id"] for g in remaining] == [project["id"]]
        assert run(capsys, "grants", "remove", str(project["id"]), "--yes")[0] == EXIT_OK
        assert run(capsys, "grants", "remove", "--user", "mia", "--project", "acme/web", "--yes")[0] == EXIT_NOT_FOUND


class TestRecover:
    def test_recover_creates_admin_and_logs_in(self, config: Config, route, capsys, paths: Paths,
                                               stdin) -> None:
        config.set("auth.enabled", True)
        app = make_app(config)
        with TestClient(app) as client:
            route(client)
            stdin("root-password-1\n")
            result = run_json(capsys, "auth", "recover", "--user", "root", "--password-stdin")
            assert result["createdUser"] is True and result["passwordReset"] is True
            assert result["daemon"] == "verified"
            assert creds.load(paths).api_key == result["key"]
            me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {result['key']}"}).json()
            assert me["user"]["username"] == "root" and me["user"]["role"] == "admin"
            login = client.post("/api/auth/login", json={"username": "root", "password": "root-password-1"})
            assert login.status_code == 200
            again = run_json(capsys, "auth", "recover", "--user", "root", "--no-login")
            assert again["createdUser"] is False and again["key"] != result["key"]
            assert creds.load(paths).api_key == result["key"]

    def test_recover_works_without_a_daemon(self, paths: Paths, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(HarnessClient, "is_up", lambda self, timeout=2.0: False)
        result = run_json(capsys, "auth", "recover")
        assert result["username"] == "admin" and result["daemon"] == "down"
        database = open_database(paths.db_file)
        try:
            assert auth_keys.verify_api_key(database, result["key"]).username == "admin"
        finally:
            database.close()

    def test_recover_member_needs_promote(self, auth_env: AuthEnv, route, capsys) -> None:
        route(auth_env.client)
        auth_env.make_user("mia")
        assert run(capsys, "auth", "recover", "--user", "mia")[0] == EXIT_USAGE
        result = run_json(capsys, "auth", "recover", "--user", "mia", "--promote")
        assert result["promoted"] is True
        me = auth_env.client.get("/api/auth/me", headers={"Authorization": f"Bearer {result['key']}"}).json()
        assert me["user"]["role"] == "admin"

    def test_recover_reenables_a_disabled_admin(self, auth_env: AuthEnv, route, capsys) -> None:
        route(auth_env.client)
        with auth_env.db.transaction() as conn:
            users.update(conn, auth_env.admin.id, disabled=True)
        result = run_json(capsys, "auth", "recover")
        assert auth_env.client.get("/api/auth/me",
                                   headers={"Authorization": f"Bearer {result['key']}"}).status_code == 200


class TestSetup:
    def test_non_interactive_setup_with_auth(self, config: Config, route, stdin, capsys, paths: Paths) -> None:
        with TestClient(make_app(config)) as client:
            route(client)
            stdin("root-password-1\n")
            result = run_json(capsys, "setup", "--yes", "--auth", "--admin", "root", "--admin-password-stdin",
                              "--port", "7300", "--public-url", "https://share.example.com",
                              "--trusted-proxies", "127.0.0.1/32", "--cloudflare", "--browser", "chromium",
                              "--ios", "2", "--android", "1", "--max-running", "2", "--service", "skip")
        stored = json.loads(paths.config_file.read_text())
        assert stored["auth.enabled"] is True and stored["server.port"] == 7300
        assert stored["server.publicUrl"] == "https://share.example.com"
        assert stored["server.trustedProxies"] == ["127.0.0.1/32"] and stored["server.cloudflare"] is True
        assert stored["browser.command"] == "chromium" and stored["devices.ios"] == 2
        assert "browser.instances" not in stored
        assert result["adminCreated"] is True and result["passwordSet"] is True and result["service"] == "skipped"
        assert result["key"].startswith("ehk_") and creds.load(paths).api_key == result["key"]
        database = open_database(paths.db_file)
        try:
            principal = auth_keys.verify_api_key(database, result["key"])
            assert principal.username == "root" and principal.is_admin
            root = users.get_by_username(database.conn(), "root")
            assert auth_core.verify_password(root.password_hash, "root-password-1")
        finally:
            database.close()

    def test_rerun_keeps_values_and_key(self, config: Config, route, stdin, capsys, paths: Paths) -> None:
        with TestClient(make_app(config)) as client:
            route(client)
            stdin("root-password-1\n")
            first = run_json(capsys, "setup", "--yes", "--auth", "--admin", "root", "--admin-password-stdin",
                             "--port", "7301", "--service", "skip")
            second = run_json(capsys, "setup", "--yes", "--service", "skip")
            assert second["changed"] == [] and second["admin"] == "root"
            assert second["keptExistingKey"] is True and second["key"] is None
            assert second["settings"]["server.port"] == 7301 and second["passwordSet"] is False
            third = run_json(capsys, "setup", "--yes", "--service", "skip", "--new-key")
            assert third["key"] and third["key"] != first["key"]
            assert creds.load(paths).api_key == third["key"]

    def test_member_name_is_not_taken_over(self, config: Config, route, capsys, paths: Paths) -> None:
        with TestClient(make_app(config)) as client:
            route(client)
            create_user(client.app.state.ctx.db, "mia", "member", "mia-password")
            code, _, err = run(capsys, "setup", "--yes", "--auth", "--admin", "mia", "--service", "skip")
            assert code == EXIT_USAGE and "member" in err

    def test_exposure_is_refused_without_force(self, config: Config, route, capsys, paths: Paths) -> None:
        with TestClient(make_app(config)) as client:
            route(client)
            code, _, err = run(capsys, "setup", "--yes", "--host", "0.0.0.0", "--no-auth", "--service", "skip")
            assert code == EXIT_USAGE and "authentication disabled" in err
            assert not paths.config_file.exists() or "server.host" not in json.loads(paths.config_file.read_text())
            forced = run_json(capsys, "setup", "--yes", "--host", "0.0.0.0", "--no-auth", "--force",
                              "--service", "skip")
            assert forced["settings"]["server.host"] == "0.0.0.0" and forced["key"] is None

    def test_invalid_flag_value(self, config: Config, route, capsys) -> None:
        with TestClient(make_app(config)) as client:
            route(client)
            assert run(capsys, "setup", "--yes", "--port", "70000", "--service", "skip")[0] == EXIT_USAGE
            assert run(capsys, "setup", "--yes", "--host", "nowhere", "--service", "skip")[0] == EXIT_USAGE

    def test_service_install_and_start(self, config: Config, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple] = []
        fake = types.ModuleType("eks_harness.service")
        state = {"installed": False}
        fake.installed = lambda: state["installed"]

        def install(paths=None):
            calls.append(("install", paths))
            state["installed"] = True
            return {"where": "test"}

        fake.install = install
        fake.start = lambda: calls.append(("start",)) or True
        monkeypatch.setitem(sys.modules, "eks_harness.service", fake)
        assert setup_cmd.manage_service("auto", paths, config, None, None, True) == "installed"
        assert calls == [("install", paths)]
        assert setup_cmd.manage_service("auto", paths, config, None, None, True) == "started"
        assert setup_cmd.manage_service("start", paths, config, None, None, True) == "started"
        assert setup_cmd.manage_service("skip", paths, config, None, None, True) == "skipped"
        state["installed"] = False
        assert setup_cmd.manage_service("start", paths, config, None, None, True) == "not-installed"

    def test_running_daemon_is_asked_to_restart(self, auth_env: AuthEnv, route, paths: Paths) -> None:
        route(auth_env.client)
        config = auth_env.app.state.ctx.config
        assert setup_cmd.manage_service("auto", paths, config, auth_env.admin_key, "http://testserver",
                                        False) == "running"
        state = setup_cmd.manage_service("auto", paths, config, auth_env.admin_key, "http://testserver", True)
        assert state in ("restarting", "restart-failed")

    def test_lan_addresses_are_ips(self) -> None:
        import ipaddress
        for address in proxies.lan_addresses():
            assert not ipaddress.ip_address(address).is_loopback
        values = [s.value for s in setup_cmd.host_suggestions()]
        assert values[:2] == ["127.0.0.1", "0.0.0.0"]


def test_admin_password_from_env(config: Config, route, capsys, paths: Paths,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(setup_cmd.ADMIN_PASSWORD_ENV, "env-password-1")
    with TestClient(make_app(config)) as client:
        route(client)
        result = run_json(capsys, "setup", "--yes", "--auth", "--service", "skip")
        assert result["admin"] == "admin" and result["passwordSet"] is True
        client.app.state.ctx.config.reload()
        login = client.post("/api/auth/login", json={"username": "admin", "password": "env-password-1"})
        assert login.status_code == 200
    database = open_database(paths.db_file)
    try:
        assert [k.prefix for k in api_keys.list_keys(database.conn())] == [result["keyPrefix"]]
    finally:
        database.close()
