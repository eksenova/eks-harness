from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from eks_harness.api.errors import ApiError
from eks_harness.auth.core import Principal, local_principal, verify_cookie
from eks_harness.auth.keys import bearer_token, verify_api_key
from eks_harness.auth.proxies import lan_addresses
from eks_harness.config import Config
from eks_harness.db import Database

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})
CROSS_SITE = frozenset({"cross-site", "same-site"})
LAN_CACHE_SECONDS = 60.0


SHARE_PREFIXES = ("/s/", "/d/", "/t/", "/assets/", "/fonts/", "/api/shared/", "/api/health")
SHARE_FILES = ("/favicon.ico", "/favicon.svg", "/robots.txt", "/manifest.webmanifest")


def share_path(path: str) -> bool:
    return path.startswith(SHARE_PREFIXES) or path in SHARE_FILES


def host_name(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


ROBOTS_TAG = "noindex, nofollow, noarchive, nosnippet, noimageindex"


class RobotsTag:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_tag(message):
            if message.get("type") == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"x-robots-tag"]
                headers.append((b"x-robots-tag", ROBOTS_TAG.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_tag)


class RequestGuard:
    def __init__(self, app: ASGIApp, config: Config) -> None:
        self.app = app
        self.config = config
        self._lan: tuple[float, frozenset[str]] = (0.0, frozenset())
        self._lan_lock = threading.Lock()

    def lan_names(self) -> frozenset[str]:
        with self._lan_lock:
            stamp, names = self._lan
            if time.monotonic() - stamp < LAN_CACHE_SECONDS and stamp:
                return names
        names = frozenset(address.lower() for address in lan_addresses())
        with self._lan_lock:
            self._lan = (time.monotonic(), names)
        return names

    def allowed_hosts(self) -> set[str]:
        names = set(LOOPBACK_NAMES)
        listen = str(self.config["server.host"] or "").strip().lower()
        if listen in WILDCARD_HOSTS:
            names |= self.lan_names()
        else:
            names.add(listen)
        public = urlsplit(self.config.public_url()).hostname
        if public:
            names.add(public.lower())
        share = urlsplit(self.config.share_url()).hostname
        if share:
            names.add(share.lower())
        names |= {str(item).strip().lower() for item in self.config["server.allowedHosts"] or []}
        return names

    def allowed_origins(self, host_header: str) -> set[str]:
        origins = {f"http://{host_header}".lower(), f"https://{host_header}".lower()}
        public = origin_of(self.config.public_url())
        if public:
            origins.add(public)
        return origins

    def app_redirect(self, scope: Scope) -> str | None:
        if scope["type"] != "http" or str(scope.get("method") or "GET").upper() not in ("GET", "HEAD"):
            return None
        share_host = self.config.share_only_host()
        if not share_host or host_name(Headers(scope=scope).get("host", "")) != share_host:
            return None
        path = str(scope.get("path") or "/")
        if share_path(path) or path.startswith("/api/") or path == "/":
            return None
        query = scope.get("query_string") or b""
        return self.config.public_url().rstrip("/") + path + (("?" + query.decode("latin-1")) if query else "")

    def problem(self, scope: Scope) -> tuple[int, str, str] | None:
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if host_name(host) not in self.allowed_hosts():
            return (421, "host_not_allowed",
                    f"This daemon does not answer to the host name {host_name(host) or '(none)'}. Open it through "
                    f"its own address, or add the name to server.allowedHosts.")
        share_host = self.config.share_only_host()
        if share_host and host_name(host) == share_host and not share_path(str(scope.get("path") or "/")):
            return 404, "not_found", "This address only serves shared files."

        method = str(scope.get("method") or "GET").upper()
        if scope["type"] == "http" and method in SAFE_METHODS:
            return None
        if (headers.get("sec-fetch-site") or "").lower() in CROSS_SITE:
            return 403, "cross_site_request", "Requests from other sites cannot change anything on this daemon."
        origin = headers.get("origin")
        if origin is not None and origin.strip().lower() not in self.allowed_origins(host):
            return 403, "cross_site_request", "Requests from other origins cannot change anything on this daemon."
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        redirect = self.app_redirect(scope)
        if redirect is not None:
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", redirect.encode("utf-8")), (b"content-length", b"0"),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        problem = self.problem(scope)
        if problem is None:
            await self.app(scope, receive, send)
            return
        status, error, message = problem
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": error})
            return
        body = json.dumps({"error": error, "message": message}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})


def authenticate(db: Database, config: Config, connection: HTTPConnection) -> Principal | None:
    if not config["auth.enabled"]:
        return local_principal(db)
    token = bearer_token(connection)
    if token:
        principal = verify_api_key(db, token)
        if principal is None:
            raise ApiError(401, "invalid_api_key", "The API key is unknown, revoked or malformed.",
                           headers={"WWW-Authenticate": "Bearer"})
        return principal
    return verify_cookie(db, connection)
