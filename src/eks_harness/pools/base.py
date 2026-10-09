from __future__ import annotations

import re

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eks_harness.config import Config
from eks_harness.paths import Paths

log = logging.getLogger("eks_harness.pools")

BROWSER_PREFIX = "browser:"
DEVICE_KINDS = ("ios", "android")
TRANSITIONAL_BACKEND_STATES = ("preparing", "starting", "stopping")


class PoolError(RuntimeError):
    pass


class BackendError(RuntimeError):
    pass


def browser_resource(index: int, profile: int) -> str:
    return f"browser:{index}:{profile}"


def parse_browser_resource(resource: str) -> tuple[int, int]:
    parts = resource.split(":")
    if len(parts) != 3 or parts[0] != "browser" or not parts[1].isdigit() or not parts[2].isdigit():
        raise ValueError(f"not a browser profile resource: {resource}")
    return int(parts[1]), int(parts[2])


def device_key(kind: str, index: int) -> str:
    return f"{kind}:{index}"


def parse_device_key(key: str) -> tuple[str, int]:
    kind, _, index = key.partition(":")
    if kind not in DEVICE_KINDS or not index.isdigit():
        raise ValueError(f"not a device resource: {key}")
    return kind, int(index)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


BindingSource = Callable[[str], list[str]]
EventSink = Callable[..., Any]


class PoolHost:
    def __init__(self, config: Config, paths: Paths, fake: bool = False) -> None:
        self.config = config
        self.paths = paths
        self.fake = fake
        self.lock = threading.RLock()
        self.devices: dict[str, dict] = {}
        self.browsers: dict[str, dict] = {}
        self.backends: dict[str, dict] = {}
        self.url = ""
        self.managed = False
        self.binding_source: BindingSource = lambda backend_id: []
        self.event_sink: EventSink | None = None
        self._threads: list[threading.Thread] = []
        self.plugins: Any = None
        self.home.mkdir(parents=True, exist_ok=True)

    def plugin_host(self) -> Any:
        if self.plugins is None:
            from eks_harness.plugins import PluginHost

            self.plugins = PluginHost(self.paths, self.config)
        return self.plugins

    @property
    def home(self) -> Path:
        return self.paths.log_dir

    @property
    def state_file(self) -> Path:
        return self.paths.pools_state_file

    def log(self, message: str) -> None:
        log.info(message)

    def emit(self, type: str, **fields: Any) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(type, **fields)
            except Exception:
                log.exception("pool event sink failed")

    def spawn(self, target: Callable[..., Any], *args: Any) -> threading.Thread:
        thread = threading.Thread(target=self._guard, args=(target, *args), daemon=True,
                                  name=f"pool-{getattr(target, '__name__', 'task')}")
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()
        return thread

    def _guard(self, target: Callable[..., Any], *args: Any) -> None:
        try:
            target(*args)
        except Exception:
            log.exception("pool task %s failed", getattr(target, "__name__", target))

    def join(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.time()))

    def bindings_for_backend(self, backend_id: str) -> list[str]:
        try:
            return list(self.binding_source(backend_id))
        except Exception:
            log.exception("backend binding source failed")
            return []

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps({"devices": self.devices, "browsers": self.browsers,
                                          "backends": self.backends}, default=str))

    def save(self) -> None:
        with self.lock:
            data = {"devices": self.devices, "browsers": self.browsers, "backends": self.backends,
                    "saved": iso_now(), "fake": self.fake}
            text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.state_file)

    def load(self) -> str:
        source = "none"
        data: dict = {}
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                source = "state"
            except (OSError, json.JSONDecodeError):
                data = {}
        with self.lock:
            self.devices = dict(data.get("devices") or {})
            self.browsers = dict(data.get("browsers") or {})
            self.backends = dict(data.get("backends") or {})
            for record in self.backends.values():
                if record.get("status") in TRANSITIONAL_BACKEND_STATES:
                    record["adopt"] = True
        return source


class LiveSource(ABC):
    content_type = "image/jpeg"

    @abstractmethod
    def frames(self) -> Iterator[bytes]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class BrowserPool(ABC):
    def __init__(self, host: PoolHost) -> None:
        self.host = host

    @property
    def config(self) -> Config:
        return self.host.config

    @abstractmethod
    def capacity(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def allocate(self, taken: set[str]) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def ensure(self, index: int) -> str:
        raise NotImplementedError

    @abstractmethod
    def alive(self, index: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    def stop(self, index: int, reason: str) -> None:
        raise NotImplementedError

    def stop_all(self, reason: str) -> None:
        for index in list(self.host.browsers):
            self.stop(int(index), reason)

    @abstractmethod
    def log_path(self, index: int) -> Path:
        raise NotImplementedError

    @abstractmethod
    def user_data_dir(self, index: int) -> Path:
        raise NotImplementedError

    def indices(self) -> list[int]:
        return list(range(1, int(self.config["browser.instances"]) + 1))

    def profile_ids(self) -> list[str]:
        per = int(self.config["browser.profilesPerInstance"])
        return [browser_resource(i, p) for i in self.indices() for p in range(1, per + 1)]

    def profile_ids_of(self, index: int) -> list[str]:
        per = int(self.config["browser.profilesPerInstance"])
        return [browser_resource(index, p) for p in range(1, per + 1)]

    def cdp_url(self, index: int) -> str | None:
        record = self.host.browsers.get(str(index)) or {}
        return record.get("cdp") if self.alive(index) else None

    @abstractmethod
    def processes(self) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def reset(self, resource: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, resource: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def close_profile(self, resource: str, context_ids: list[str]) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def sweep(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def live_source(self, resource: str) -> LiveSource:
        raise NotImplementedError


class DevicePool(ABC):
    def __init__(self, host: PoolHost) -> None:
        self.host = host

    @property
    def config(self) -> Config:
        return self.host.config

    @abstractmethod
    def define(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def available(self, kind: str) -> tuple[bool, str]:
        raise NotImplementedError

    @abstractmethod
    def allocate(self, kind: str, taken: set[str]) -> str | None:
        raise NotImplementedError

    def running_count(self, exclude: str | None = None) -> int:
        return sum(1 for k, d in self.host.devices.items()
                   if k != exclude and d.get("status") in ("on", "booting"))

    @abstractmethod
    def boot(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def shutdown(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def clean_app(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def log_path(self, key: str) -> Path:
        raise NotImplementedError

    @abstractmethod
    def sweep(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def screenshot(self, key: str, dest: Path) -> dict:
        raise NotImplementedError

    @abstractmethod
    def record_start(self, key: str, dest: Path) -> None:
        raise NotImplementedError

    @abstractmethod
    def record_stop(self, key: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def is_recording(self, key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def record_reset(self, key: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def live_source(self, key: str) -> LiveSource:
        raise NotImplementedError

    @abstractmethod
    def device_log(self, key: str, since: float | None, dest: Path) -> dict:
        raise NotImplementedError


class BackendManager(ABC):
    def __init__(self, host: PoolHost) -> None:
        self.host = host

    @property
    def records(self) -> dict[str, dict]:
        return self.host.backends

    @staticmethod
    def backend_id(definition: str, instance: str) -> str:
        return f"{definition}@{instance}"

    def get(self, backend_id: str) -> dict | None:
        return self.records.get(backend_id)

    def list(self) -> list[dict]:
        return [self.public(r) for r in list(self.records.values())]

    @abstractmethod
    def definitions(self, tree: str | None = None) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def find_definition(self, name: str, tree: str | None) -> Path:
        raise NotImplementedError

    @abstractmethod
    def ensure(self, body: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    def stop(self, backend_id: str, final: bool = False, reason: str = "") -> None:
        raise NotImplementedError

    def hold(self, backend_id: str, seconds: float, hold_id: str | None = None) -> dict:
        record = self.records.get(backend_id)
        if record is None:
            raise BackendError(f"no backend {backend_id}")
        with self.host.lock:
            holds = record.setdefault("holds", {})
            if seconds > 0:
                holds[hold_id or "manual"] = time.time() + seconds
            elif hold_id:
                holds.pop(hold_id, None)
            else:
                holds.clear()
            self.host.save()
        return self.public(record)

    def bindings(self, record: dict) -> list[str]:
        now = time.time()
        found = self.host.bindings_for_backend(record["id"])
        found += [f"hold {k}" for k, until in (record.get("holds") or {}).items() if until > now]
        return found

    def tick(self) -> None:
        now = time.time()
        to_stop = []
        with self.host.lock:
            for record in self.records.values():
                record["holds"] = {k: v for k, v in (record.get("holds") or {}).items() if v > now}
                if record.get("status") not in ("running", "failed"):
                    continue
                if self.bindings(record):
                    record.pop("emptySince", None)
                    continue
                record.setdefault("emptySince", now)
                grace = float(record.get("idleGraceSeconds") or self.host.config["backend.idleGraceSeconds"])
                if now - record["emptySince"] > grace:
                    to_stop.append(record["id"])
        for backend_id in to_stop:
            self.stop(backend_id, final=False, reason="no frontend bound")

    def instance_ended(self, instance: str) -> None:
        for backend_id in [r["id"] for r in list(self.records.values()) if r.get("instance") == instance]:
            self.stop(backend_id, final=True, reason="harness instance ended")

    def reconcile(self) -> list[str]:
        with self.host.lock:
            pending = [r["id"] for r in self.records.values() if r.pop("adopt", None)
                       and r.get("status") in TRANSITIONAL_BACKEND_STATES]
            self.host.save()
        for backend_id in pending:
            self.host.log(f"backend {backend_id}: re-adopting it in state {self.records[backend_id]['status']}")
            self.host.spawn(self.adopt, backend_id)
        return pending

    @abstractmethod
    def adopt(self, backend_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def log_path(self, record: dict) -> Path:
        raise NotImplementedError

    @abstractmethod
    def process_alive(self, record: dict, name: str) -> bool:
        raise NotImplementedError

    def public(self, record: dict) -> dict:
        out = {k: v for k, v in record.items() if k != "holds"}
        out["bindings"] = self.bindings(record)
        out["logFile"] = str(self.log_path(record))
        out["alive"] = {name: self.process_alive(record, name) for name in record.get("processes") or []}
        out["holds"] = {k: v for k, v in (record.get("holds") or {}).items()}
        return out


class Pools:
    def __init__(self, host: PoolHost, browsers: BrowserPool, devices: DevicePool, backends: BackendManager) -> None:
        self.host = host
        self.browsers = browsers
        self.devices = devices
        self.backends = backends

    @property
    def fake(self) -> bool:
        return self.host.fake


def device_name(config, kind: str, index: int) -> str:
    """Pool device name: '<prefix> iOS N' for simulators, '<prefix>_harness_N' (lower case; 'harness_N' for the default prefix) for Android AVDs."""

    prefix = str(config["devices.namePrefix"] or "").strip() or "Harness"
    if kind == "ios":
        return f"{prefix} iOS {index}"
    slug = re.sub(r"[^a-z0-9]+", "_", prefix.lower()).strip("_") or "harness"
    return f"{slug}_{index}" if slug == "harness" else f"{slug}_harness_{index}"


def parse_device_name(config, name: str) -> tuple[str, int] | None:
    """Inverse of device_name for this config's prefix."""

    for kind in ("ios", "android"):
        pattern = re.escape(device_name(config, kind, 0))[:-1] + r"(\d+)"
        match = re.fullmatch(pattern, name.strip())
        if match:
            return kind, int(match.group(1))
    return None
