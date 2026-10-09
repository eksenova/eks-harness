from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from eks_harness._version import build_info
from eks_harness.api.links import Links
from eks_harness.config import RESTART_REQUIRED, Config
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.paths import Paths
from eks_harness.plugins import PluginHost
from eks_harness.pools import Pools


class AppContext:
    def __init__(self, *, config: Config, paths: Paths, db: Database, events: EventBus, pools: Pools,
                 managed: bool = False, plugins: PluginHost | None = None) -> None:
        self.config = config
        self.paths = paths
        self.db = db
        self.events = events
        self.pools = pools
        self.plugins = plugins or PluginHost(paths, config)
        self.links = Links(config)
        self.managed = managed
        self.started_at = time.time()
        self.pid = os.getpid()
        self.started_config = config.as_dict()
        self.version = build_info()
        self.lock = threading.RLock()
        self.services: dict[str, Any] = {}
        self.restart_handler: Callable[[], None] | None = None
        self.stop_handler: Callable[[], None] | None = None
        self.control_token: str | None = None
        self.url = config.local_url()

    @property
    def fake_pools(self) -> bool:
        return self.pools.fake

    @property
    def auth_enabled(self) -> bool:
        return bool(self.config["auth.enabled"])

    def register_service(self, name: str, service: Any) -> Any:
        with self.lock:
            self.services[name] = service
        return service

    def service(self, name: str) -> Any:
        with self.lock:
            if name not in self.services:
                raise LookupError(f"service '{name}' is not registered")
            return self.services[name]

    def has_service(self, name: str) -> bool:
        with self.lock:
            return name in self.services

    def restart_pending_keys(self) -> list[str]:
        current = self.config.as_dict()
        return sorted(k for k in RESTART_REQUIRED if current.get(k) != self.started_config.get(k))

    def request_restart(self) -> bool:
        if self.restart_handler is None:
            return False
        self.restart_handler()
        return True

    def request_stop(self) -> bool:
        if self.stop_handler is None:
            return False
        self.stop_handler()
        return True
