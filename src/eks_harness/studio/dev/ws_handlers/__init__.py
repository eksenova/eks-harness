"""Per-topic WebSocket handler modules.

Each handler module registers callables via
``eks_harness.studio.dev.ws_endpoint.register_handler``. M1 ships only
``system`` (subscribe / unsubscribe / ping); later milestones add
``render``, ``preview``, ``media``, ``timeline``, ``artifacts``.
"""

from __future__ import annotations

__all__: list[str] = []
