from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import JsonValue

from eks_harness.engine.errors import DriverError
from eks_harness.engine.model import LeaseHandle, RunInfo
from eks_harness.ir.plan import Node, SessionSpec

log = logging.getLogger(__name__)

CallPhase = Literal["run", "enter", "exit"]
SnapshotKind = Literal["screen", "dom", "a11y"]
FileRole = Literal["value", "shot", "evidence"]


@dataclass(frozen=True, slots=True)
class DriverFile:
    path: Path
    role: FileRole = "evidence"
    kind: str | None = None
    name: str | None = None
    caption: str = ""


@dataclass(frozen=True, slots=True)
class CallSite:
    node: Node
    instance: str = ""
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class DriverReply:
    value: JsonValue = None
    files: tuple[DriverFile, ...] = field(default_factory=tuple[DriverFile, ...])
    detail: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])


class DriverPort(Protocol):
    @property
    def platform(self) -> str: ...

    @property
    def simulated(self) -> bool: ...

    def call(self, op: str, args: Mapping[str, JsonValue], *, site: CallSite, phase: CallPhase = "run",
             timeout: float | None = None) -> DriverReply: ...

    def snapshot(self, kinds: Sequence[SnapshotKind], dest: Path) -> list[DriverFile]: ...

    def version(self) -> str: ...

    def close(self) -> None: ...


class DriverProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def platforms(self) -> frozenset[str]: ...

    def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> DriverPort: ...


class DriverRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, DriverProvider] = {}

    def register(self, provider: DriverProvider) -> DriverProvider:
        platforms = frozenset(provider.platforms)
        if not platforms:
            raise ValueError(f"driver provider {provider.name!r} serves no platform")
        with self._lock:
            for platform in platforms:
                self._providers[platform] = provider
        return provider

    def provider(self, platform: str) -> DriverProvider | None:
        with self._lock:
            return self._providers.get(platform)

    def platforms(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._providers)


class DriverPorts:
    def __init__(self, providers: Mapping[str, DriverProvider], run: RunInfo,
                 sessions: Mapping[str, SessionSpec]) -> None:
        self.providers = dict(providers)
        self.run = run
        self.sessions = dict(sessions)
        self._lock = threading.Lock()
        self._ports: dict[str, DriverPort] = {}
        self._opening: dict[str, threading.Event] = {}
        self._closed = False

    def port(self, lease: LeaseHandle) -> DriverPort | None:
        resource_id = lease.resource_id
        while True:
            with self._lock:
                self._require_open()
                found = self._ports.get(resource_id)
                if found is not None:
                    return found
                provider = self.providers.get(lease.platform)
                if provider is None:
                    return None
                pending = self._opening.get(resource_id)
                if pending is None:
                    opening = threading.Event()
                    self._opening[resource_id] = opening
                    break
            pending.wait()
        try:
            opened = provider.open(lease, self.sessions.get(lease.session_key), self.run)
        except BaseException:
            with self._lock:
                self._opening.pop(resource_id, None)
            opening.set()
            raise
        with self._lock:
            self._opening.pop(resource_id, None)
            discard = self._closed
            if not discard:
                self._ports[resource_id] = opened
        opening.set()
        if discard:
            self._close_port(resource_id, opened)
            self._require_open()
        return opened

    def open_all(self, leases: Sequence[LeaseHandle]) -> None:
        if not leases:
            return
        with ThreadPoolExecutor(max_workers=len(leases), thread_name_prefix="eks-open") as pool:
            for future in [pool.submit(self.port, lease) for lease in leases]:
                future.result()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            ports = list(self._ports.items())
            self._ports.clear()
        for resource_id, port in ports:
            self._close_port(resource_id, port)

    def _close_port(self, resource_id: str, port: DriverPort) -> None:
        try:
            port.close()
        except Exception:
            log.exception("closing the driver port of lease %s of run %s failed", resource_id, self.run.id)

    def _require_open(self) -> None:
        if self._closed:
            raise DriverError("driver_error", f"the driver ports of run {self.run.id} are closed")


__all__ = [
    "CallPhase",
    "CallSite",
    "DriverFile",
    "DriverPort",
    "DriverPorts",
    "DriverProvider",
    "DriverRegistry",
    "DriverReply",
    "FileRole",
    "SnapshotKind",
]
