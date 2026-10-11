from __future__ import annotations

import contextvars
import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from eks_harness.cli.client import HarnessClient
from eks_harness.mcp_context import (
    REQUEST_TREE,
    TREE_HEADER,
    TREE_QUERY,
    remote_request,
)
from eks_harness.mcp_server import SERVER_NAME, HarnessTools, create_server

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eks_harness.daemon.context import AppContext
    from eks_harness.plugins import PluginHost

log = logging.getLogger("eks_harness.mcp")

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


class PluginToolRegistry:
    def __init__(self, host: PluginHost, tools: HarnessTools) -> None:
        self.host = host
        self.tools = tools
        self._lock = threading.Lock()
        self._servers: dict[Path | None, tuple[tuple[tuple[str, str | None], ...], MCPServer | None]] = {}

    def current(self) -> MCPServer | None:
        requested = REQUEST_TREE.get()
        try:
            return self.for_tree(Path(requested) if requested else None)
        except Exception as error:
            log.warning("plugin MCP tools unavailable for %s: %s", requested or "the hub", error)
            return None

    def for_tree(self, tree: Path | None) -> MCPServer | None:
        from eks_harness.plugins import find_tree

        if tree is not None:
            tree = find_tree(tree) or tree.resolve()
        records = [r for r in self.host.active(tree) if r.manifest.of_type("mcp_tools")]
        signature = tuple((c.key, r.digest) for r in records for c in r.manifest.of_type("mcp_tools"))
        with self._lock:
            cached = self._servers.get(tree)
            if cached is not None and cached[0] == signature:
                return cached[1]
        server = self._build(tree) if signature else None
        with self._lock:
            self._servers[tree] = (signature, server)
        return server

    def _build(self, tree: Path | None) -> MCPServer:
        from mcp.server.mcpserver import MCPServer

        server = MCPServer(SERVER_NAME, log_level="WARNING")
        for contribution, target in self.host.load_all("mcp_tools", tree):
            try:
                target.register(server, self.tools)
            except Exception as error:
                log.warning("mcp tools %s failed for %s: %s", contribution.key, tree or "the hub", error)
        return server


def requested_tree(connection: HTTPConnection) -> str | None:
    value = connection.headers.get(TREE_HEADER) or connection.query_params.get(TREE_QUERY) or ""
    return value.strip() or None


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
        tree = requested_tree(connection)
        if tree is not None and not Path(tree).expanduser().is_dir():
            body = json.dumps({"error": "no_tree", "message": f"No directory {tree} on the hub machine."}).encode()
            await send({"type": "http.response.start", "status": 400,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        reset = TOKEN.set(token or None)
        try:
            with remote_request(tree):
                await self.app(scope, receive, send)
        finally:
            TOKEN.reset(reset)


def build(ctx: AppContext) -> tuple[Any, Any]:
    tools = RequestTools(ctx.config.local_url())
    registry = PluginToolRegistry(ctx.plugins, tools)
    server, _ = create_server(lambda: HarnessClient(ctx.config.local_url()), tools=tools,
                              plugin_tools=registry.current)
    from mcp.server.transport_security import TransportSecuritySettings

    starlette_app = server.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True, host=str(ctx.config["server.host"]),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return _AuthGate(starlette_app, ctx), starlette_app
