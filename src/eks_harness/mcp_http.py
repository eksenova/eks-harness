from __future__ import annotations

import contextvars
import json
import threading
from typing import TYPE_CHECKING, Any

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from eks_harness.cli.client import HarnessClient
from eks_harness.mcp_server import HarnessTools, create_server

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

TOKEN: contextvars.ContextVar[str | None] = contextvars.ContextVar("eks_harness_mcp_token", default=None)
MOUNT = "/mcp"


class RequestTools(HarnessTools):
    def __init__(self, base_url: str) -> None:
        super().__init__(lambda: HarnessClient(base_url))
        self.base_url = base_url
        self._clients: dict[str, HarnessClient] = {}
        self._clients_lock = threading.Lock()

    @property
    def client(self) -> HarnessClient:
        token = TOKEN.get() or ""
        with self._clients_lock:
            client = self._clients.get(token)
            if client is None:
                client = HarnessClient(self.base_url, api_key=token or None)
                self._clients[token] = client
            return client

    def close(self) -> None:
        with self._clients_lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()


class _AuthGate:
    def __init__(self, app: ASGIApp, ctx: AppContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        from eks_harness.auth import middleware as auth_middleware

        connection = HTTPConnection(scope)
        header = connection.headers.get("authorization") or ""
        token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        try:
            principal = auth_middleware.authenticate(self.ctx.db, self.ctx.config, connection)
        except Exception:
            principal = None
        if self.ctx.config["auth.enabled"] and (principal is None or not token):
            body = json.dumps({"error": "unauthorized",
                               "message": "Send an API key: Authorization: Bearer ehk_..."}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        reset = TOKEN.set(token or None)
        try:
            await self.app(scope, receive, send)
        finally:
            TOKEN.reset(reset)


def build(ctx: AppContext) -> tuple[Any, Any]:
    tools = RequestTools(ctx.config.local_url())
    server, _ = create_server(lambda: HarnessClient(ctx.config.local_url()), tools=tools)
    from mcp.server.transport_security import TransportSecuritySettings

    starlette_app = server.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True, host=str(ctx.config["server.host"]),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return _AuthGate(starlette_app, ctx), starlette_app
