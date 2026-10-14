from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from pydantic import JsonValue

import eks
from eks_harness.engine.auth import (
    ANY_DEFINITION,
    AuthRequest,
    BackendHookAuth,
    CredentialsAuth,
    PersonaAuthRegistry,
)
from eks_harness.engine.errors import PersonaAuthError
from eks_harness.engine.model import BackendHandle, BackendIdentity, RunInfo
from eks_harness.ir.registry import Registry, contributing, default_registry
from eks_harness.plugins.sdk import BackendInfo, BackendSpec


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


SEEN: list[tuple[str, BackendInfo]] = []


class _Base:
    def describe(self) -> dict[str, Any]:
        return {}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return ""

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec()

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


class HookedBackend(_Base):
    @staticmethod
    def persona_headers(persona: str, info: BackendInfo) -> Mapping[str, str] | None:
        SEEN.append((persona, info))
        if persona == "nobody":
            return None
        return {"X-Tenant": str(info.extra.get("tenant")), "Authorization": f"Hook {persona}"}


class ClassHookBackend(_Base):
    prefix = "Class"

    @classmethod
    def persona_headers(cls, persona: str, info: BackendInfo) -> Mapping[str, str] | None:
        return {"Authorization": f"{cls.prefix} {persona} {info.url}"}


class BrokenHookBackend(_Base):
    @staticmethod
    def persona_headers(persona: str, info: BackendInfo) -> object:
        return ["not", "a", "mapping"]


class PlainBackend(_Base):
    pass


class Recording:
    def __init__(self, answers: Mapping[str, Mapping[str, str] | None]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[str, str, int]] = []

    def headers(self, request: AuthRequest) -> Mapping[str, str] | None:
        self.calls.append((request.run.id, request.persona, request.backend.identity.generation))
        return self.answers.get(request.persona)


def run_info(run_id: str = "01RUN") -> RunInfo:
    return RunInfo(id=run_id, project="acme/web", session="main", plan_hash="sha256:" + "0" * 64, plan_kind="flow",
                   plan_name="f", created_at=1.0)


def handle(definition: str = "testapi", secrets: Mapping[str, JsonValue] | None = None, *, generation: int = 1,
           backend_id: str | None = None) -> BackendHandle:
    identity = BackendIdentity(definition=definition, backend_id=backend_id or f"{definition}@tree:x",
                               lease_id="bl_1", source="plan", generation=generation)
    return BackendHandle(resource_id="rbackend", url="http://127.0.0.1:1", identity=identity,
                         info={"url": "http://127.0.0.1:1"}, secrets=dict(secrets or {}))


def request_for(persona: str, backend: BackendHandle, registry: Registry, run_id: str = "01RUN") -> AuthRequest:
    return AuthRequest(persona=persona, backend=backend, session=None, run=run_info(run_id), registry=registry,
                       tree=None)


def basic(header: str) -> tuple[str, str]:
    scheme, _, encoded = header.partition(" ")
    assert scheme == "Basic"
    user, _, password = base64.b64decode(encoded).decode().partition(":")
    return user, password


def test_credentials_auth_forms(registry: Registry) -> None:
    secrets: dict[str, JsonValue] = {"url": "http://127.0.0.1:1", "credentials": {
        "raw": {"headers": {"X-Session": "abc", "X-Tenant": "harman"}},
        "bearer": {"token": "tok-123"},
        "admin": {"username": "admin", "password": "p:ss word"},
        "service": {"api_key": "key-1"},
        "custom": {"api_key": "key-2", "header": "X-Custom-Key"},
        "secretive": {"password": {"$secret": "hidden"}, "username": "sec"},
        "empty": {},
        "personas": {"admin": {"token": "from-personas"}},
    }}
    auth = CredentialsAuth()
    backend = handle(secrets=secrets)
    assert auth.headers(request_for("raw", backend, registry)) == {"X-Session": "abc", "X-Tenant": "harman"}
    assert auth.headers(request_for("bearer", backend, registry)) == {"Authorization": "Bearer tok-123"}
    assert auth.headers(request_for("service", backend, registry)) == {"X-Api-Key": "key-1"}
    assert auth.headers(request_for("custom", backend, registry)) == {"X-Custom-Key": "key-2"}
    assert auth.headers(request_for("admin", backend, registry)) == {"Authorization": "Bearer from-personas"}
    secret_headers = auth.headers(request_for("secretive", backend, registry))
    assert secret_headers is not None and basic(secret_headers["Authorization"]) == ("sec", "hidden")
    assert auth.headers(request_for("empty", backend, registry)) is None
    assert auth.headers(request_for("missing", backend, registry)) is None
    assert auth.headers(request_for("personas", backend, registry)) is None
    plain = handle(secrets={"credentials": {"admin": {"username": "admin", "password": "p:ss word"}}})
    plain_headers = auth.headers(request_for("admin", plain, registry))
    assert plain_headers is not None and basic(plain_headers["Authorization"]) == ("admin", "p:ss word")
    assert auth.headers(request_for("admin", handle(secrets={}), registry)) is None


def test_backend_hook_auth_calls_the_static_hook(registry: Registry) -> None:
    SEEN.clear()
    eks.backend("hooked")(HookedBackend)
    eks.backend("classy")(ClassHookBackend)
    eks.backend("plain")(PlainBackend)
    eks.backend("broken")(BrokenHookBackend)
    secrets: dict[str, JsonValue] = {"url": "http://127.0.0.1:9", "extra": {"tenant": "harman"},
                                     "credentials": {"admin": {"password": {"$secret": "pw"}}}}
    auth = BackendHookAuth()
    answer = auth.headers(request_for("admin", handle("hooked", secrets), registry))
    assert answer == {"X-Tenant": "harman", "Authorization": "Hook admin"}
    persona, info = SEEN[-1]
    assert persona == "admin" and isinstance(info, BackendInfo)
    assert info.url == "http://127.0.0.1:9" and info.credentials == {"admin": {"password": "pw"}}
    assert auth.headers(request_for("nobody", handle("hooked", secrets), registry)) is None
    assert auth.headers(request_for("admin", handle("classy", secrets), registry)) == {
        "Authorization": "Class admin http://127.0.0.1:9"}
    assert auth.headers(request_for("admin", handle("plain", secrets), registry)) is None
    assert auth.headers(request_for("admin", handle("unregistered", secrets), registry)) is None
    with pytest.raises(PersonaAuthError, match="returned list, not a mapping"):
        auth.headers(request_for("admin", handle("broken", secrets), registry))


def test_order_providers_hook_then_credentials(registry: Registry) -> None:
    eks.backend("hooked")(HookedBackend)
    secrets: dict[str, JsonValue] = {"extra": {"tenant": "t1"},
                                     "credentials": {"admin": {"token": "cred"}, "viewer": {"token": "view"}}}
    backend = handle("hooked", secrets)
    auth = PersonaAuthRegistry()
    assert auth.headers(request_for("admin", backend, registry)) == {"X-Tenant": "t1", "Authorization": "Hook admin"}
    nobody_auth = PersonaAuthRegistry()
    with pytest.raises(PersonaAuthError) as missing:
        nobody_auth.headers(request_for("nobody", backend, registry))
    assert str(missing.value) == ("backend hooked offers no credentials for persona nobody; add them to the backend "
                                  "info credentials or register a persona auth provider")
    assert missing.value.code == "persona_auth"

    ordered = PersonaAuthRegistry()
    anywhere_first = Recording({"admin": {"Authorization": "anywhere-1"}})
    anywhere_second = Recording({"admin": {"Authorization": "anywhere-2"}, "viewer": {"Authorization": "any-v"}})
    exact = Recording({"admin": {"Authorization": "exact"}})
    other = Recording({"admin": {"Authorization": "other-definition"}})
    ordered.register(anywhere_first)
    ordered.register(anywhere_second, definition=ANY_DEFINITION)
    ordered.register(other, definition="elsewhere")
    ordered.register(exact, definition="hooked")
    assert ordered.headers(request_for("admin", backend, registry)) == {"Authorization": "exact"}
    assert other.calls == []
    assert ordered.headers(request_for("viewer", backend, registry)) == {"Authorization": "any-v"}
    assert [call[1] for call in anywhere_first.calls] == ["viewer"]

    falls_through = PersonaAuthRegistry()
    falls_through.register(Recording({}))
    assert falls_through.headers(request_for("viewer", handle("plainapi", secrets), registry)) == {
        "Authorization": "Bearer view"}

    assert ordered.can_authenticate(request_for("admin", backend, registry)) is True
    assert nobody_auth.can_authenticate(request_for("nobody", backend, registry)) is False
    assert nobody_auth.can_authenticate(request_for("ghost", handle("plainapi", {}), registry)) is False


def test_provider_errors_become_persona_auth_errors(registry: Registry) -> None:
    class Exploding:
        def headers(self, request: AuthRequest) -> Mapping[str, str] | None:
            raise RuntimeError("vault offline")

    class Bad:
        def headers(self, request: AuthRequest) -> Mapping[str, str] | None:
            return {"X-Number": {"nested": 1}}  # pyright: ignore[reportReturnType]

    auth = PersonaAuthRegistry()
    auth.register(Exploding())
    with pytest.raises(PersonaAuthError, match="Exploding failed for persona admin on backend testapi: vault offline"):
        auth.headers(request_for("admin", handle(), registry))
    assert auth.can_authenticate(request_for("admin", handle(), registry)) is False
    bad = PersonaAuthRegistry()
    bad.register(Bad())
    with pytest.raises(PersonaAuthError, match="header names to text values"):
        bad.headers(request_for("admin", handle(), registry))

    class ListProvider:
        def headers(self, request: AuthRequest) -> Mapping[str, str] | None:
            return [("Authorization", "Bearer x")]  # pyright: ignore[reportReturnType]

    listed = PersonaAuthRegistry()
    listed.register(ListProvider())
    with pytest.raises(PersonaAuthError) as wrong:
        listed.headers(request_for("admin", handle(), registry))
    assert wrong.value.message == ("persona auth provider ListProvider returned list, not a mapping of header names "
                                   "to values")
    assert listed.can_authenticate(request_for("admin", handle(), registry)) is False


def test_cache_per_run_backend_generation_and_persona(registry: Registry) -> None:
    provider = Recording({"admin": {"Authorization": "cached"}})
    auth = PersonaAuthRegistry()
    auth.register(provider)
    first = handle(generation=1)
    assert auth.headers(request_for("admin", first, registry, "run-a")) == {"Authorization": "cached"}
    assert auth.headers(request_for("admin", first, registry, "run-a")) == {"Authorization": "cached"}
    assert provider.calls == [("run-a", "admin", 1)]
    assert auth.can_authenticate(request_for("admin", first, registry, "run-a"))
    assert len(provider.calls) == 1
    auth.headers(request_for("admin", handle(generation=2), registry, "run-a"))
    assert provider.calls[-1] == ("run-a", "admin", 2)
    auth.headers(request_for("admin", handle(backend_id="testapi@other"), registry, "run-a"))
    assert len(provider.calls) == 3
    auth.headers(request_for("admin", first, registry, "run-b"))
    assert len(provider.calls) == 4
    auth.forget("run-a")
    auth.headers(request_for("admin", first, registry, "run-b"))
    assert len(provider.calls) == 4
    auth.headers(request_for("admin", first, registry, "run-a"))
    assert len(provider.calls) == 5
    returned = auth.headers(request_for("admin", first, registry, "run-a"))
    assert isinstance(returned, dict)
    returned["Authorization"] = "mutated"
    assert auth.headers(request_for("admin", first, registry, "run-a")) == {"Authorization": "cached"}


def test_hook_and_credentials_answers_are_cached_too(registry: Registry) -> None:
    SEEN.clear()
    with contributing("acme.api", "1.0.0"):
        eks.backend("hooked")(HookedBackend)
    auth = PersonaAuthRegistry()
    backend = handle("hooked", {"extra": {"tenant": "t"}})
    auth.headers(request_for("admin", backend, registry))
    auth.headers(request_for("admin", backend, registry))
    assert len(SEEN) == 1
    auth.forget("01RUN")
    auth.headers(request_for("admin", backend, registry))
    assert len(SEEN) == 2
