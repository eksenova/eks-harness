"""Connection registry + topic-subscription pub/sub for the dev server.

Each connected websocket gets a bounded ``asyncio.Queue`` (drop-oldest on
overflow) so a slow consumer cannot block producers. Topics are simple
strings; the hub broadcasts a message to every subscriber whose subscription
set includes the broadcast topic (or a project-scoped variant like
``jobs:<pid>``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = ["WSHub", "ClientId", "WSClient"]

_LOG = logging.getLogger(__name__)

ClientId = str

_QUEUE_MAX = 256


@dataclass
class WSClient:
    """One connected websocket, with its outbox queue and topic set."""

    id: ClientId
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAX)
    )
    topics: set[str] = field(default_factory=set)


class WSHub:
    """Per-process registry of connected dev-server websockets."""

    # Topic constants - handlers should prefer these over bare strings.
    TOPIC_SYSTEM = "system"
    TOPIC_PROJECTS = "projects"
    TOPIC_CATALOG = "catalog"
    TOPIC_MEDIA = "media"
    TOPIC_JOBS = "jobs"
    TOPIC_LOG = "log"

    ALL_TOPICS = (
        TOPIC_SYSTEM,
        TOPIC_PROJECTS,
        TOPIC_CATALOG,
        TOPIC_MEDIA,
        TOPIC_JOBS,
        TOPIC_LOG,
    )

    def __init__(self) -> None:
        self._clients: dict[ClientId, WSClient] = {}
        self._lock = asyncio.Lock()

    async def register(self) -> WSClient:
        """Allocate a fresh client id + outbox queue."""

        client_id = uuid.uuid4().hex[:12]
        client = WSClient(id=client_id)
        async with self._lock:
            self._clients[client_id] = client
        _LOG.debug("hub.register %s", client_id)
        return client

    async def unregister(self, client_id: ClientId) -> None:
        """Drop the client and discard any unread messages in its queue."""

        async with self._lock:
            self._clients.pop(client_id, None)
        _LOG.debug("hub.unregister %s", client_id)

    async def subscribe(self, client_id: ClientId, topics: list[str]) -> set[str]:
        """Add ``topics`` to the client's subscription set; return the new set."""

        async with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return set()
            client.topics.update(topics)
            return set(client.topics)

    async def unsubscribe(self, client_id: ClientId, topics: list[str]) -> set[str]:
        """Remove ``topics`` from the client's subscription set; return remainder."""

        async with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return set()
            for topic in topics:
                client.topics.discard(topic)
            return set(client.topics)

    def topics_for(self, client_id: ClientId) -> set[str]:
        client = self._clients.get(client_id)
        return set(client.topics) if client else set()

    async def broadcast(self, topic: str, message: dict[str, Any]) -> None:
        """Deliver ``message`` to every client subscribed to ``topic``.

        Topics are matched as-is. Project-scoped variants (e.g. ``jobs:<pid>``)
        are filtered server-side by the caller - pass the scoped string here
        and clients receive it iff they subscribed with the same scope.
        """

        # Copy under lock; deliver outside the lock to avoid holding it while
        # we wait on queue ops.
        async with self._lock:
            targets = [
                client for client in self._clients.values() if topic in client.topics
            ]
        for client in targets:
            self._enqueue(client, message)

    async def send_to(self, client_id: ClientId, message: dict[str, Any]) -> bool:
        """Direct-send to one client. Returns False if the client is gone."""

        async with self._lock:
            client = self._clients.get(client_id)
        if client is None:
            return False
        self._enqueue(client, message)
        return True

    def _enqueue(self, client: WSClient, message: dict[str, Any]) -> None:
        """Put on the client's queue; drop the oldest item on overflow."""

        try:
            client.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        # Drop oldest, then retry. Best-effort - if the consumer is wedged
        # we keep dropping, but never block the producer.
        try:
            _ = client.queue.get_nowait()
            client.queue.task_done()
        except Exception:  # pragma: no cover - defensive
            return
        try:
            client.queue.put_nowait(message)
        except asyncio.QueueFull:  # pragma: no cover - defensive
            _LOG.warning("dropped message for %s; queue still full", client.id)
