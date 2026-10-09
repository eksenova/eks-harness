"""M1 handlers: ``subscribe``, ``unsubscribe``, ``ping``.

Each handler returns a ``ReplyEnvelope``. The endpoint dispatcher serialises
it and writes it back over the websocket.
"""

from __future__ import annotations

import time

from ..ws_hub import WSHub
from ..ws_protocol import (
    PingRequest,
    ReplyEnvelope,
    SubscribeRequest,
    UnsubscribeRequest,
)

__all__ = ["handle_subscribe", "handle_unsubscribe", "handle_ping", "register"]


async def handle_subscribe(
    request: SubscribeRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope:
    """Add the requested topics to the client's subscription set."""

    topics = await hub.subscribe(client_id, request.payload.topics)
    return ReplyEnvelope(
        type="subscribe.reply",
        id=request.id,
        result={"topics": sorted(topics)},
    )


async def handle_unsubscribe(
    request: UnsubscribeRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope:
    """Remove the requested topics from the client's subscription set."""

    topics = await hub.unsubscribe(client_id, request.payload.topics)
    return ReplyEnvelope(
        type="unsubscribe.reply",
        id=request.id,
        result={"topics": sorted(topics)},
    )


async def handle_ping(
    request: PingRequest, *, hub: WSHub, client_id: str
) -> ReplyEnvelope:
    """Lightweight keep-alive."""

    return ReplyEnvelope(
        type="ping.reply",
        id=request.id,
        result={"ts": time.time()},
    )


def register(register_handler) -> None:
    """Wire M1 handlers into the endpoint dispatch registry."""

    register_handler("subscribe", handle_subscribe)
    register_handler("unsubscribe", handle_unsubscribe)
    register_handler("ping", handle_ping)
