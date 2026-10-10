from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading
from typing import TYPE_CHECKING

from eks_harness.db.repos import nodes as repo
from eks_harness.nodes.agent import NodeAgent, NodeSettings
from eks_harness.nodes.hub import NODE_ID, NodeError, hub

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

log = logging.getLogger("eks_harness.node")

SERVICE = "host-node"
HOST_FLAG = "host"


def host_node_id(configured: str = "") -> str:
    if configured:
        return configured
    name = socket.gethostname().split(".", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", name).strip("-") or "hub"
    return slug[:63] if NODE_ID.fullmatch(slug[:63]) else "hub"


class HostNode:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.node_id = host_node_id(str(ctx.config["nodes.hostNodeId"] or ""))
        self.agent: NodeAgent | None = None
        self.thread: threading.Thread | None = None

    def _token(self) -> str | None:
        nodes = hub(self.ctx)
        existing = repo.get_node(self.ctx.db.conn(), self.node_id)
        if existing is None:
            _, token = nodes.create_node(self.node_id, label="This hub machine", config={HOST_FLAG: True})
            return token
        if not existing.config.get(HOST_FLAG):
            log.warning("node %s already belongs to another machine; set nodes.hostNodeId to run this machine "
                        "as a node", self.node_id)
            return None
        return nodes.rotate_token(self.node_id)

    def start(self) -> None:
        try:
            token = self._token()
        except NodeError as error:
            log.warning("the host node could not be registered: %s", error)
            return
        if token is None:
            return
        settings = NodeSettings(hub=self.ctx.config.local_url(), token=token, name=self.node_id,
                                cache_dir=str(self.ctx.paths.cache_dir / "host-node"))
        self.agent = NodeAgent(settings, self.ctx.paths)
        self.thread = threading.Thread(target=self._run, name="host-node", daemon=True)
        self.thread.start()
        log.info("this machine runs as node %s", self.node_id)

    def _run(self) -> None:
        agent = self.agent
        if agent is None:
            return
        try:
            asyncio.run(agent.run())
        except Exception:
            log.exception("the host node stopped")

    def stop(self) -> None:
        if self.agent is not None:
            self.agent.stop()
        if self.thread is not None:
            self.thread.join(timeout=10)


def is_host(item: dict) -> bool:
    return bool((item.get("config") or {}).get(HOST_FLAG))
