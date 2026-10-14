from __future__ import annotations

import http.cookiejar
import json
import math
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

import httpx
from pydantic import JsonValue

from eks_harness._version import __version__
from eks_harness.engine.auth import AuthRequest, PersonaAuthRegistry
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    builtin_provenance,
)
from eks_harness.engine.model import BackendHandle
from eks_harness.ir.ops.api import BodyRequest, Request
from eks_harness.ops import API_EXECUTOR, BUILTIN_EXECUTOR_VERSION
from eks_harness.pools.backend_info import REDACTED, redact_info, scrub, secret_strings

API_OPS = ("api.get", "api.post", "api.put", "api.patch", "api.delete")
INFO_OP = "backend.info"
BODY_LIMIT_BYTES = 4096
DEFAULT_TIMEOUT_SECONDS = 30.0
RUN_HEADER = "X-Eks-Run"
NODE_HEADER = "X-Eks-Node"

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_SENSITIVE_WORDS = ("auth", "token", "secret", "key", "password", "session", "cookie")
_MIN_SECRET = 4
_POLL_SECONDS = 0.05
_PATH_SAFE = "!$&'()*+,;=:@/"
_SHORT_ESCAPES = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\\": "\\\\"}
_HTML_SENSITIVE = frozenset("\"&'+<>`")


def request_url(base: str, path: str) -> str:
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or path.startswith("//"):
        raise NodeFailure("args_invalid",
                          f"api path {path!r} names a scheme or host; give a path relative to the backend's base URL "
                          "such as /api/tenant-branches",
                          detail={"path": path})
    return base.rstrip("/") + "/" + path.lstrip("/")


def _query_text(value: JsonValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return json.dumps(value, allow_nan=False)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def query_params(query: Mapping[str, JsonValue] | None) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for name, value in (query or {}).items():
        if value is None:
            continue
        if isinstance(value, list):
            params.extend((name, _query_text(item)) for item in value if item is not None)
            continue
        params.append((name, _query_text(value)))
    return params


def _reject_constant(constant: str) -> JsonValue:
    raise ValueError(f"{constant} is not a JSON value")


def parse_body(response: httpx.Response) -> JsonValue:
    content = response.content
    if not content.strip():
        return None
    text = response.text
    try:
        return cast(JsonValue, json.loads(text, parse_constant=_reject_constant))
    except ValueError:
        return text


def _sensitive(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SENSITIVE_HEADERS or any(word in lowered for word in _SENSITIVE_WORDS)


def _header_secrets(value: str) -> list[str]:
    found = [value] if len(value) >= _MIN_SECRET else []
    credential = value.partition(" ")[2].strip()
    if len(credential) >= _MIN_SECRET:
        found.append(credential)
    return found


def _unicode_escape(char: str, upper: bool) -> str:
    code = ord(char)
    units = [code] if code <= 0xFFFF else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
    return "".join(f"\\u{unit:04X}" if upper else f"\\u{unit:04x}" for unit in units)


def _escaped_json(secret: str, upper: bool) -> str:
    out: list[str] = []
    for char in secret:
        if char in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) > 0x7E or char in _HTML_SENSITIVE:
            out.append(_unicode_escape(char, upper))
        else:
            out.append(char)
    return "".join(out)


def _secret_forms(secret: str) -> set[str]:
    json_forms = {
        json.dumps(secret, ensure_ascii=False)[1:-1],
        json.dumps(secret, ensure_ascii=True)[1:-1],
        _escaped_json(secret, upper=True),
        _escaped_json(secret, upper=False),
    }
    json_forms |= {form.replace("/", "\\/") for form in json_forms}
    url_forms = {quote_plus(secret, safe=""), quote(secret, safe=""), quote(secret, safe=_PATH_SAFE)}
    return {secret, *json_forms, *url_forms}


def _expanded(secrets: list[str]) -> list[str]:
    found: set[str] = set()
    for secret in secrets:
        if len(secret) >= _MIN_SECRET:
            found |= {form for form in _secret_forms(secret) if len(form) >= _MIN_SECRET}
    return sorted(found, key=len, reverse=True)


def _leaf_strings(value: JsonValue) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in _leaf_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _leaf_strings(item)]
    if value is None or isinstance(value, bool):
        return []
    text = str(value)
    return [text] if len(text) >= _MIN_SECRET else []


def _hidden_values(redacted: JsonValue, revealed: JsonValue) -> list[str]:
    if redacted == REDACTED:
        return [] if revealed == REDACTED else _leaf_strings(revealed)
    if isinstance(redacted, dict) and isinstance(revealed, dict):
        return [text for key, item in redacted.items() if key in revealed
                for text in _hidden_values(item, revealed[key])]
    if isinstance(redacted, list) and isinstance(revealed, list):
        return [text for shown, full in zip(redacted, revealed, strict=False)
                for text in _hidden_values(shown, full)]
    return []


def _backend_secrets(handle: BackendHandle) -> list[str]:
    revealed = dict(handle.secrets)
    found = _hidden_values(cast(JsonValue, dict(handle.info)), cast(JsonValue, revealed))
    found.extend(secret for secret in secret_strings(revealed) if secret)
    return list(dict.fromkeys(found))


def _without_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _excerpt(text: str | None, secrets: list[str]) -> str | None:
    if text is None:
        return None
    cleaned = scrub(text, secrets).encode("utf-8")
    if len(cleaned) <= BODY_LIMIT_BYTES:
        return cleaned.decode("utf-8")
    return cleaned[:BODY_LIMIT_BYTES].decode("utf-8", errors="ignore")


def _scrubbed(value: Any, secrets: list[str]) -> JsonValue:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, dict):
        return {str(key): _scrubbed(item, secrets) for key, item in cast(dict[Any, Any], value).items()}
    if isinstance(value, list | tuple):
        return [_scrubbed(item, secrets) for item in cast(list[Any], value)]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, bool | int | float):
        return cast(JsonValue, value)
    return scrub(str(value), secrets)


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise NodeFailure("args_invalid", f"the {what} must be text after resolution, got {type(value).__name__}")
    return value


class ApiExecutor:
    def __init__(self, auth: PersonaAuthRegistry, *, client: httpx.Client | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.auth = auth
        self.client = client
        self.timeout = timeout
        self._client_lock = threading.Lock()
        self._shared: httpx.Client | None = None

    def run(self, call: ExecContext) -> ExecResult:
        args = call.args
        if not isinstance(args, Request):
            raise NodeFailure("args_invalid", f"{call.op} needs api request arguments, got {type(args).__name__}")
        handle = call.backend()
        method = call.op.rpartition(".")[2].upper()
        url = request_url(handle.url, _text(args.path, "api path"))
        secrets = _backend_secrets(handle)
        headers = httpx.Headers({
            "Accept": "application/json",
            "User-Agent": f"eks-harness/{__version__}",
            RUN_HEADER: call.run.id,
            NODE_HEADER: f"{call.node.id}@{call.instance}" if call.instance else call.node.id,
        })
        for name, raw in (args.headers or {}).items():
            value = _text(raw, f"header {name}")
            headers[name] = value
            if _sensitive(name):
                secrets.extend(_header_secrets(value))
        persona = self.persona(call)
        if persona is not None:
            for name, value in self.auth.headers(self._auth_request(call, persona, handle)).items():
                headers[name] = value
                secrets.extend(_header_secrets(value))
        body: JsonValue = None
        if method in _BODY_METHODS and isinstance(args, BodyRequest):
            body = args.body
        sends_body = body is not None
        secrets = _expanded(secrets)
        limit, by_deadline = self._limit(call)
        client = self._client()
        params = tuple(query_params(cast(dict[str, JsonValue] | None, args.query)))
        request = httpx.Request(method, url, params=params, headers=headers, json=body if sends_body else None,
                                extensions={"timeout": httpx.Timeout(limit).as_dict()})
        shown_url = scrub(_without_userinfo(str(request.url)), secrets)
        request_text = (json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                        if sends_body else None)
        started = time.monotonic()
        try:
            response = self._send(client, request, call, limit)
        except httpx.HTTPError as error:
            elapsed = time.monotonic() - started
            said = scrub(str(error), secrets)
            reason = f"{type(error).__name__}: {said}" if said else type(error).__name__
            call.event("http", {"method": method, "url": shown_url, "status": None, "ms": round(elapsed * 1000),
                                "bytes": 0, "request": _excerpt(request_text, secrets), "response": None,
                                "error": reason})
            if isinstance(error, httpx.TimeoutException) and by_deadline:
                raise NodeFailure("timeout",
                                  f"{method} {shown_url} did not answer before the deadline of node {call.node.id} "
                                  f"({limit:.3f}s)",
                                  detail={"method": method, "url": shown_url, "seconds": round(limit, 3)}) from None
            raise NodeFailure("http_error", f"{method} {shown_url} failed: {reason}",
                              detail={"method": method, "url": shown_url, "error": type(error).__name__},
                              retryable=True) from None
        elapsed = time.monotonic() - started
        response_excerpt = _excerpt(response.text, secrets)
        call.event("http", {"method": method, "url": shown_url, "status": response.status_code,
                            "ms": round(elapsed * 1000), "bytes": len(response.content),
                            "request": _excerpt(request_text, secrets), "response": response_excerpt})
        expected = args.expect_status
        status = response.status_code
        accepted = status in expected if expected is not None else 200 <= status < 300
        if not accepted:
            wanted = ", ".join(str(code) for code in expected) if expected is not None else "any 2xx"
            raise NodeFailure("http_status", f"{method} {shown_url} returned {status}, expected {wanted}",
                              detail={"method": method, "url": shown_url, "status": status,
                                      "body": response_excerpt})
        return ExecResult(value=parse_body(response))

    def persona(self, call: ExecContext) -> str | None:
        args = call.args
        if isinstance(args, Request) and args.persona is not None:
            return args.persona
        if call.state is not None and call.state.persona:
            return call.state.persona
        return None

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(API_EXECUTOR, BUILTIN_EXECUTOR_VERSION, ops=API_OPS, run=self.run,
                            provenance=builtin_provenance(),
                            description="Calls the backend's HTTP API as the session persona")

    def _auth_request(self, call: ExecContext, persona: str, handle: BackendHandle) -> AuthRequest:
        tree = Path(call.run.tree) if call.run.tree else None
        return AuthRequest(persona=persona, backend=handle, session=call.session, run=call.run,
                           registry=call.registry, tree=tree)

    def _limit(self, call: ExecContext) -> tuple[float, bool]:
        call.check_cancelled()
        remaining = call.remaining()
        if remaining is not None and remaining < self.timeout:
            return remaining, True
        return self.timeout, False

    def _client(self) -> httpx.Client:
        if self.client is not None:
            return self.client
        with self._client_lock:
            if self._shared is None:
                refuse_all = http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
                self._shared = httpx.Client(follow_redirects=False, timeout=self.timeout,
                                            cookies=http.cookiejar.CookieJar(policy=refuse_all))
            return self._shared

    def _send(self, client: httpx.Client, request: httpx.Request, call: ExecContext, limit: float) -> httpx.Response:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"eks-api-{call.node.id}")
        try:
            future = pool.submit(client.send, request, auth=None, follow_redirects=False)
        finally:
            pool.shutdown(wait=False)
        until = time.monotonic() + limit
        while not future.done():
            if call.cancel.is_set():
                raise NodeFailure("cancelled", f"the run was cancelled while node {call.node.id} ({call.op}) waited "
                                               "for the backend")
            left = until - time.monotonic()
            if left <= 0:
                raise httpx.TimeoutException(f"no complete response within {limit:.3f}s", request=request)
            wait([future], timeout=min(_POLL_SECONDS, left))
        return future.result()


class BackendInfoExecutor:
    def run(self, call: ExecContext) -> ExecResult:
        handle = call.backend()
        identity = handle.identity
        if not handle.info:
            raise NodeFailure("resource_error",
                              f"backend {identity.backend_id} ({identity.definition}) of node {call.node.id} has no "
                              "redacted info to report",
                              detail={"backend": identity.backend_id, "resource": handle.resource_id})
        value = _scrubbed(redact_info(dict(handle.info)), _expanded(_backend_secrets(handle)))
        if not isinstance(value, dict):
            raise NodeFailure("executor_error", f"backend {identity.backend_id} has no info object")
        value["backend"] = {
            "id": identity.backend_id,
            "definition": identity.definition,
            "generation": identity.generation,
            "plugin": identity.plugin,
            "version": identity.plugin_version,
        }
        return ExecResult(value=value)

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(API_EXECUTOR, BUILTIN_EXECUTOR_VERSION, ops=(INFO_OP,), run=self.run,
                            provenance=builtin_provenance(),
                            description="Reports the redacted info and identity of the session's backend")


__all__ = [
    "API_OPS",
    "BODY_LIMIT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "INFO_OP",
    "NODE_HEADER",
    "RUN_HEADER",
    "ApiExecutor",
    "BackendInfoExecutor",
    "parse_body",
    "query_params",
    "request_url",
]
