from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eks_harness.auth import core as auth_core
from eks_harness.auth import keys as auth_keys
from eks_harness.cli.client import HarnessClient
from eks_harness.config import SETTINGS, Config, env_name
from eks_harness.daemon.app import create_app
from eks_harness.daemon.context import AppContext
from eks_harness.db import Database
from eks_harness.db.repos import grants, projects, sessions, users
from eks_harness.paths import DIR_ENVS, HOME_ENV, Paths, resolve_paths
from eks_harness.pools.fake import FAKE_ENV

ISOLATION_ENVS = (
    HOME_ENV, FAKE_ENV, "EKS_HARNESS_API_KEY", "EKS_HARNESS_URL",
    "EKS_HARNESS_WEB_DIST", "EKS_HARNESS_CACHE", *DIR_ENVS.values(), *(env_name(k) for k in SETTINGS),
)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-password-1"


@pytest.fixture(autouse=True)
def harness_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ISOLATION_ENVS:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "eks-harness-home"
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv(FAKE_ENV, "1")
    monkeypatch.setenv("EKS_HARNESS_WEB_DIST", str(tmp_path / "no-web-ui"))
    monkeypatch.setenv(env_name("server.allowedHosts"), '["testserver"]')
    return home


@pytest.fixture
def paths(harness_home: Path) -> Paths:
    resolved = resolve_paths()
    assert resolved.isolated
    assert str(resolved.data_dir).startswith(str(harness_home))
    return resolved.ensure()


@pytest.fixture
def config(paths: Paths) -> Config:
    return Config(paths)


def make_app(config: Config) -> FastAPI:
    return create_app(config, config.paths, fake_pools=True)


@pytest.fixture
def app(config: Config) -> FastAPI:
    return make_app(config)


@pytest.fixture
def ctx(app: FastAPI) -> AppContext:
    return app.state.ctx


@pytest.fixture
def db(ctx: AppContext) -> Database:
    return ctx.db


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def create_user(db: Database, username: str, role: str = "member", password: str | None = None) -> users.User:
    password_hash = auth_core.hash_password(password) if password else None
    with db.transaction() as conn:
        return users.create(conn, username, role=role, password_hash=password_hash)


def create_key(db: Database, user_id: int, name: str = "test") -> str:
    raw, _ = auth_keys.create_api_key(db, user_id, name)
    return raw


def ensure_project(db: Database, project_id: str) -> projects.Project:
    with db.transaction() as conn:
        project, _ = projects.ensure(conn, project_id)
    return project


def ensure_session(db: Database, project_id: str, name: str) -> sessions.Session:
    ensure_project(db, project_id)
    with db.transaction() as conn:
        session, _ = sessions.ensure_in_project(conn, project_id, name)
    return session


def add_grant(db: Database, user_id: int, project_id: str, level: str = "viewer",
              session_name: str | None = None) -> grants.Grant:
    ensure_project(db, project_id)
    session_id = ensure_session(db, project_id, session_name).id if session_name else None
    with db.transaction() as conn:
        return grants.create(conn, user_id, project_id, level, session_id)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@dataclass
class AuthEnv:
    app: FastAPI
    client: TestClient
    db: Database
    admin: users.User
    admin_key: str
    admin_password: str = ADMIN_PASSWORD
    members: dict[str, tuple[users.User, str]] = field(default_factory=dict)

    @property
    def admin_headers(self) -> dict[str, str]:
        return bearer(self.admin_key)

    def make_user(self, username: str, role: str = "member", password: str | None = None) -> tuple[users.User, str]:
        user = create_user(self.db, username, role, password)
        key = create_key(self.db, user.id, f"{username} test key")
        self.members[username] = (user, key)
        return user, key

    def grant(self, user: users.User, project_id: str, level: str = "viewer",
              session_name: str | None = None) -> grants.Grant:
        return add_grant(self.db, user.id, project_id, level, session_name)

    def headers_for(self, key: str) -> dict[str, str]:
        return bearer(key)


@pytest.fixture
def auth_config(paths: Paths) -> Config:
    cfg = Config(paths)
    cfg.set("auth.enabled", True)
    return cfg


@pytest.fixture
def auth_env(auth_config: Config) -> Iterator[AuthEnv]:
    auth_app = make_app(auth_config)
    database = auth_app.state.ctx.db
    admin = create_user(database, ADMIN_USERNAME, "admin", ADMIN_PASSWORD)
    admin_key = create_key(database, admin.id, "admin test key")
    with TestClient(auth_app) as test_client:
        yield AuthEnv(app=auth_app, client=test_client, db=database, admin=admin, admin_key=admin_key)


def pytest_configure(config: pytest.Config) -> None:
    os.environ[FAKE_ENV] = "1"
    os.environ[HOME_ENV] = tempfile.mkdtemp(prefix="eks-harness-tests-")


HOP_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection", "content-encoding"})


class TestClientTransport(httpx.BaseTransport):
    __test__ = False

    def __init__(self, test_client: TestClient) -> None:
        self.test_client = test_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in HOP_HEADERS]
        answer = self.test_client.request(request.method, str(request.url), headers=headers, content=body or None)
        reply_headers = [(k, v) for k, v in answer.headers.multi_items() if k.lower() not in HOP_HEADERS]
        return httpx.Response(answer.status_code, headers=reply_headers, content=answer.content, request=request)


def harness_client(test_client: TestClient, config: Config, api_key: str | None = None) -> HarnessClient:
    return HarnessClient(base_url="http://testserver", api_key=api_key or "", paths=config.paths, config=config,
                         transport=TestClientTransport(test_client))
