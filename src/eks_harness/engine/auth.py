from __future__ import annotations

import base64
import importlib
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from eks_harness.engine.errors import PersonaAuthError
from eks_harness.engine.model import BackendHandle, RunInfo
from eks_harness.ir.plan import SessionSpec
from eks_harness.ir.registry import Registry, RegistryError
from eks_harness.plugins.sdk import BackendInfo

PERSONA_HOOK = "persona_headers"
PERSONAS_KEY = "personas"
ANY_DEFINITION = "*"


@dataclass(frozen=True, slots=True)
class AuthRequest:
    persona: str
    backend: BackendHandle
    session: SessionSpec | None
    run: RunInfo
    registry: Registry
    tree: Path | None


class PersonaAuth(Protocol):
    def headers(self, request: AuthRequest) -> Mapping[str, str] | None: ...


_DEFAULT_API_KEY_HEADER = "X-Api-Key"


def _text_headers(value: Mapping[Any, Any], where: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or isinstance(item, Mapping | list | tuple) or item is None:
            raise PersonaAuthError(f"{where} must map header names to text values; {key!r} is not usable")
        out[key] = str(item)
    return out


def persona_entry(credentials: Mapping[str, Any], persona: str) -> Mapping[str, Any] | None:
    personas = credentials.get(PERSONAS_KEY)
    if isinstance(personas, Mapping) and isinstance(entry := personas.get(persona), Mapping):
        return entry
    entry = credentials.get(persona) if persona != PERSONAS_KEY else None
    return entry if isinstance(entry, Mapping) else None


class CredentialsAuth:
    def headers(self, request: AuthRequest) -> Mapping[str, str] | None:
        credentials = BackendInfo.from_dict(request.backend.secrets).credentials
        entry = persona_entry(credentials, request.persona)
        if entry is None:
            return None
        where = f"credentials of persona {request.persona} on backend {request.backend.identity.definition}"
        headers = entry.get("headers")
        if isinstance(headers, Mapping) and headers:
            return _text_headers(headers, f"the headers of the {where}")
        token = entry.get("token")
        if isinstance(token, str) and token:
            return {"Authorization": f"Bearer {token}"}
        username, password = entry.get("username"), entry.get("password")
        if isinstance(username, str) and username and isinstance(password, str):
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        api_key = entry.get("api_key")
        if isinstance(api_key, str) and api_key:
            header = entry.get("header")
            name = header if isinstance(header, str) and header else _DEFAULT_API_KEY_HEADER
            return {name: api_key}
        return None


def _implementation(reference: str) -> object | None:
    module_name, _, qualname = reference.partition(":")
    if not module_name or not qualname:
        return None
    found: object | None = sys.modules.get(module_name)
    if found is None:
        try:
            found = importlib.import_module(module_name)
        except ImportError:
            return None
    for part in qualname.split("."):
        found = getattr(found, part, None)
        if found is None:
            return None
    return found


class BackendHookAuth:
    def headers(self, request: AuthRequest) -> Mapping[str, str] | None:
        definition = request.backend.identity.definition
        try:
            spec = request.registry.backend(definition)
        except RegistryError:
            return None
        impl = _implementation(spec.impl)
        hook = getattr(impl, PERSONA_HOOK, None) if impl is not None else None
        if not callable(hook):
            return None
        call: Callable[[str, BackendInfo], object] = hook
        result = call(request.persona, BackendInfo.from_dict(request.backend.secrets))
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise PersonaAuthError(f"the {PERSONA_HOOK} hook of backend {definition} returned "
                                   f"{type(result).__name__}, not a mapping of header names to values")
        return _text_headers(result, f"the {PERSONA_HOOK} answer of backend {definition}")


class PersonaAuthRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: list[tuple[str, PersonaAuth]] = []
        self._cache: dict[tuple[str, str, int, str], Mapping[str, str]] = {}

    def register(self, provider: PersonaAuth, *, definition: str = ANY_DEFINITION) -> None:
        if not definition:
            raise ValueError("register a persona auth provider for a backend definition name or '*'")
        with self._lock:
            self._providers.append((definition, provider))

    def _chain(self, definition: str) -> list[PersonaAuth]:
        with self._lock:
            registered = list(self._providers)
        exact = [provider for name, provider in registered if name == definition and name != ANY_DEFINITION]
        anywhere = [provider for name, provider in registered if name == ANY_DEFINITION]
        return [*exact, *anywhere, BackendHookAuth(), CredentialsAuth()]

    def headers(self, request: AuthRequest) -> Mapping[str, str]:
        identity = request.backend.identity
        key = (request.run.id, identity.backend_id, identity.generation, request.persona)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return dict(cached)
        for provider in self._chain(identity.definition):
            try:
                found: object = provider.headers(request)
            except PersonaAuthError:
                raise
            except Exception as error:
                raise PersonaAuthError(
                    f"persona auth provider {type(provider).__name__} failed for persona {request.persona} on "
                    f"backend {identity.definition}: {error}") from error
            if found is None:
                continue
            if not isinstance(found, Mapping):
                raise PersonaAuthError(
                    f"persona auth provider {type(provider).__name__} returned {type(found).__name__}, not a mapping "
                    "of header names to values")
            headers = _text_headers(cast(Mapping[Any, Any], found), f"the answer of persona auth provider {type(provider).__name__}")
            with self._lock:
                self._cache[key] = headers
            return dict(headers)
        raise PersonaAuthError(
            f"backend {identity.definition} offers no credentials for persona {request.persona}; add them to the "
            "backend info credentials or register a persona auth provider")

    def can_authenticate(self, request: AuthRequest) -> bool:
        try:
            self.headers(request)
        except PersonaAuthError:
            return False
        return True

    def forget(self, run_id: str) -> None:
        with self._lock:
            for key in [key for key in self._cache if key[0] == run_id]:
                del self._cache[key]


__all__ = [
    "ANY_DEFINITION",
    "PERSONAS_KEY",
    "PERSONA_HOOK",
    "AuthRequest",
    "BackendHookAuth",
    "CredentialsAuth",
    "PersonaAuth",
    "PersonaAuthRegistry",
    "persona_entry",
]
