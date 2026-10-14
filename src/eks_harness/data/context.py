from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from eks_harness.data.ids import Ids
from eks_harness.ir.refs import RefExpr, is_expr_dict
from eks_harness.plugins.sdk import BackendInfo

SECRET_KEYS = frozenset({"password", "token", "secret", "api_key", "apiKey", "headers", "authorization",
                         "Authorization", "access_token", "refresh_token"})
REDACTED = "<redacted>"
BODY_EXCERPT = 800


class SeedError(RuntimeError):
    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = dict(detail or {})


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: REDACTED if key in SECRET_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


Auth = Callable[[str], Mapping[str, str]]


class SeedApi:
    def __init__(self, base_url: str, *, auth: Auth, timeout: float = 120.0,
                 client: httpx.Client | None = None) -> None:
        if not base_url:
            raise SeedError("the backend has no url, so seed resources cannot call its API")
        self.base_url = base_url.rstrip("/")
        self._auth = auth
        self._client = client or httpx.Client(timeout=timeout)
        self._own = client is None

    def close(self) -> None:
        if self._own:
            self._client.close()

    def url(self, path: str) -> str:
        if "://" in path:
            raise SeedError(f"seed API paths are relative to the backend url, got {path!r}")
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(self, method: str, path: str, *, json_body: Any = None, query: Mapping[str, Any] | None = None,
                headers: Mapping[str, str] | None = None, as_: str | None = None,
                expect: tuple[int, ...] | None = None) -> Any:
        url = self.url(path)
        sent = {"Accept": "application/json", "User-Agent": "eks-harness-seed"}
        if as_:
            sent.update(self._auth(as_))
        sent.update(headers or {})
        try:
            response = self._client.request(method, url, json=json_body, params=_query(query), headers=sent)
        except httpx.HTTPError as error:
            raise SeedError(f"{method} {url} failed: {error}", detail={"method": method, "url": url}) from error
        ok = response.status_code in expect if expect else 200 <= response.status_code < 300
        if not ok:
            raise SeedError(f"{method} {url} answered {response.status_code}: {response.text[:BODY_EXCERPT]}",
                            detail={"method": method, "url": url, "status": response.status_code,
                                    "body": response.text[:BODY_EXCERPT]})
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json_body=json, **kwargs)

    def put(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, json_body=json, **kwargs)

    def patch(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return self.request("PATCH", path, json_body=json, **kwargs)


def _query(query: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not query:
        return None
    out: dict[str, Any] = {}
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, Mapping):
            out[key] = json.dumps(value, separators=(",", ":"))
        else:
            out[key] = value
    return out


class ResourceContext:
    def __init__(self, *, kind: str, key: str | None, path: str, phase: str, parent: Any,
                 fields: Mapping[str, Any], raw_fields: Mapping[str, Any], api: SeedApi | None,
                 backend: BackendInfo, now: datetime, seed: int, log: Callable[[str], None]) -> None:
        self.kind = kind
        self.key = key
        self.path = path
        self.phase = phase
        self.parent = parent
        self.fields = dict(fields)
        self._raw = dict(raw_fields)
        self._api = api
        self.backend = backend
        self.now = now
        self.seed = seed
        self.random = random.Random(seed)
        self.ids = Ids(seed, now)
        self._log = log

    @property
    def api(self) -> SeedApi:
        if self._api is None:
            raise SeedError(f"resource {self.path}: the backend has no url, so ctx.api is not available")
        return self._api

    def resolve(self, value: Any) -> Any:
        target = value.model_dump(mode="json", by_alias=True) if isinstance(value, RefExpr) else value
        if isinstance(target, BaseModel) or not is_expr_dict(target):
            return value
        for name, raw in self._raw.items():
            if raw == target:
                return self.fields[name]
            if isinstance(raw, list):
                for index, item in enumerate(raw):
                    if item == target:
                        return self.fields[name][index]
        raise SeedError(f"resource {self.path}: {target!r} is not one of its reference fields")

    def log(self, message: str) -> None:
        self._log(f"{self.path}: {message}")


__all__ = ["REDACTED", "SECRET_KEYS", "ResourceContext", "SeedApi", "SeedError", "redact"]
