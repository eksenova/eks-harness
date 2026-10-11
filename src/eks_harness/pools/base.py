from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eks_harness.config import Config
from eks_harness.ids import is_backend_lease_id, new_backend_lease_id
from eks_harness.paths import Paths
from eks_harness.pools.backend_info import (
    NEVER,
    backend_identity,
    describe_duration,
    duration_seconds,
    normalize_info,
    parse_duration,
    redact_info,
    reveal_info,
)

log = logging.getLogger("eks_harness.pools")

BROWSER_PREFIX = "browser:"
DEVICE_KINDS = ("ios", "android")
TRANSITIONAL_BACKEND_STATES = ("preparing", "starting", "stopping")
IDLE_STATES = ("running", "failed", "stopped")
VERIFY_INTERVAL = 15.0
RESTART_GRACE = 120.0
BACKEND_LEASE_EVENT = "backend.lease"
BACKEND_REPLACED_EVENT = "backend.replaced"


class PoolError(RuntimeError):
    pass


class BackendError(RuntimeError):
    pass


class BackendLeaseGone(BackendError):
    def __init__(self, lease_id: str) -> None:
        super().__init__(f"no backend lease {lease_id}: it was released or expired")
        self.lease_id = lease_id


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
        self.link_alive: Callable[[str], bool] = lambda sid: True
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


@dataclass(frozen=True)
class AppTargets:
    ios: tuple[str, ...] = ()
    android: tuple[str, ...] = ()


class DevicePool(ABC):
    def __init__(self, host: PoolHost) -> None:
        self.host = host
        self.stray_patterns: Callable[[], list[str]] = lambda: []

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
    def clean_app(self, key: str, apps: AppTargets) -> None:
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


def new_lease(record: dict, body: dict, ttl: float | None, stamp: float) -> dict:
    return {"id": new_backend_lease_id(),
            "kind": body.get("kind") or ("driver" if body.get("driverSid") else "agent"),
            "holder": str(body.get("holder") or body.get("owner") or "anonymous"), "owner": body.get("owner"),
            "project": body.get("project"), "session": body.get("session"), "tree": body.get("holderTree"),
            "driverSid": body.get("driverSid"), "userId": body.get("userId"), "created": stamp, "renewed": stamp,
            "ttl": ttl,
            "expires": stamp + ttl if ttl else None}


def renew_lease(lease: dict, ttl: float | None, stamp: float) -> None:
    lease["renewed"] = stamp
    lease["ttl"] = ttl
    lease["expires"] = stamp + ttl if ttl else None


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

    def definition_dirs(self, tree: str | None) -> list[tuple[str, Path]]:
        places = [("tree", Path(tree) / relative) for relative in self.tree_definition_dirs(tree)] if tree else []
        places.append(("config", self.host.paths.backends_dir))
        return places

    def tree_definition_dirs(self, tree: str) -> list[str]:
        configured = self.project_backends(tree).get("definition_dirs")
        if isinstance(configured, list) and all(isinstance(item, str) for item in configured):
            return configured
        return list(self.host.config["backend.definitionDirs"] or [])

    def project_backends(self, tree: str | None) -> dict:
        if not tree:
            return {}
        from eks_harness.plugins.project import ProjectConfigError

        try:
            return self.host.plugin_host().project(Path(tree)).section("backends")
        except ProjectConfigError:
            return {}

    @abstractmethod
    def definitions(self, tree: str | None = None) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def find_definition(self, name: str, tree: str | None) -> Path:
        raise NotImplementedError

    @abstractmethod
    def bring_up(self, backend_id: str, plan: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self, backend_id: str, final: bool = False, reason: str = "") -> None:
        raise NotImplementedError

    @abstractmethod
    def adopt(self, backend_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def log_path(self, record: dict) -> Path:
        raise NotImplementedError

    @abstractmethod
    def process_alive(self, record: dict, name: str) -> bool:
        raise NotImplementedError

    def readopt(self, backend_id: str) -> None:
        return None

    def sweep_orphans(self) -> list[str]:
        return []

    def check_running(self) -> None:
        return None

    def lease_ttl(self, value: Any) -> float | None:
        if value is None:
            return float(self.host.config["backend.leaseTtlSeconds"])
        parsed = parse_duration(value)
        if parsed == NEVER:
            return None
        if float(parsed) <= 0:
            raise BackendError("a lease ttl must be positive (or never)")
        return float(parsed)

    def emit_lease(self, action: str, record: dict, lease: dict, **detail: Any) -> None:
        self.host.emit(BACKEND_LEASE_EVENT, resource=f"backend:{record['id']}", lease_sid=lease.get("driverSid"),
                       actor=lease.get("owner") or lease.get("holder"),
                       detail={"action": action, "lease": lease["id"], "backend": record["id"],
                               "kind": lease.get("kind"), "holder": lease.get("holder"),
                               "leases": len(record.get("leases") or {}), **detail})

    def identity_of(self, body: dict) -> str:
        if body.get("instance") and not body.get("key"):
            return str(body["instance"])
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise BackendError("params must be an object")
        return backend_identity(body.get("tree"), body.get("key"), params)

    def acquire(self, body: dict) -> dict:
        definition = str(body.get("definition") or "")
        if not definition:
            raise BackendError("definition is required")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise BackendError("params must be an object")
        tree = str(Path(body["tree"]).expanduser().resolve()) if body.get("tree") else None
        identity = self.identity_of({**body, "tree": tree})
        backend_id = body.get("id") or self.backend_id(definition, identity)
        with_lease = not body.get("noLease")
        ttl = None if body.get("driverSid") else (self.lease_ttl(body.get("ttl")) if with_lease else None)
        idle_request = None
        if body.get("idleTimeout") not in (None, ""):
            try:
                idle_request = parse_duration(body["idleTimeout"])
            except ValueError as error:
                raise BackendError(str(error)) from error
        definition_path = self.find_definition(definition, tree)
        stamp = time.time()
        lease: dict | None = None
        with self.host.lock:
            record = self.records.get(backend_id)
            if record is None:
                record = {"id": backend_id, "definition": definition, "instance": identity, "status": "stopped",
                          "created": stamp, "generation": 0, "leases": {}}
                self.records[backend_id] = record
            elif record.get("definition") != definition:
                raise BackendError(f"backend {backend_id} runs {record.get('definition')}, not {definition}")
            record["tree"] = tree or record.get("tree")
            record["definitionPath"] = str(definition_path)
            record["key"] = record.get("instance") or identity
            record["params"] = params or record.get("params") or {}
            if idle_request is not None:
                record["idleTimeoutRequest"] = idle_request
            record.setdefault("leases", {})
            if with_lease:
                lease = self.find_reusable(record, body)
                if lease is None:
                    lease = new_lease(record, body, ttl, stamp)
                    record["leases"][lease["id"]] = lease
                else:
                    renew_lease(lease, ttl, stamp)
                record.pop("idleSince", None)
            self.resolve_idle_timeout(record)
            plan = self.plan(record, bool(body.get("restart")))
            self.host.save()
        if lease is not None:
            self.emit_lease("acquired", record, lease)
        if plan:
            self.bring_up(backend_id, plan)
        return self.lease_response(backend_id, lease["id"] if lease else None)

    def find_reusable(self, record: dict, body: dict) -> dict | None:
        leases = record.get("leases") or {}
        if body.get("leaseId") and body["leaseId"] in leases:
            return leases[body["leaseId"]]
        if body.get("driverSid"):
            return next((lease for lease in leases.values() if lease.get("driverSid") == body["driverSid"]), None)
        if body.get("reuseHolder"):
            kind, holder = body.get("kind") or "agent", body.get("holder")
            return next((lease for lease in leases.values()
                         if lease.get("kind") == kind and lease.get("holder") == holder), None)
        return None

    def plan(self, record: dict, restart: bool) -> str | None:
        status = record.get("status") or "stopped"
        if status in ("stopped", "failed"):
            record["status"] = "preparing"
            record.pop("error", None)
            return "start"
        if status == "running":
            if restart:
                return "restart"
            if record.get("checking") or time.time() - float(record.get("verifiedAt") or 0) < VERIFY_INTERVAL:
                return None
            record["checking"] = True
            return "verify"
        if status == "stopping":
            record["resume"] = True
        return None

    def resolve_idle_timeout(self, record: dict, described: dict | None = None) -> None:
        if described is not None:
            for key in ("idleTimeout", "idle_timeout", "idleGraceSeconds"):
                if described.get(key) is not None:
                    try:
                        record["definitionIdleTimeout"] = parse_duration(described[key])
                    except ValueError:
                        self.host.log(f"backend {record['id']}: ignoring {key}={described[key]!r} from describe")
                    break
        candidates: list[tuple[str, Any]] = [("request", record.get("idleTimeoutRequest"))]
        section = self.project_backends(record.get("tree"))
        own = section.get(record["definition"])
        if isinstance(own, dict):
            candidates.append(("project", own.get("idle_timeout")))
        candidates.append(("project", section.get("idle_timeout")))
        candidates.append(("definition", record.get("definitionIdleTimeout")))
        candidates.append(("definition", record.get("idleGraceSeconds")))
        candidates.append(("definition", self.manifest_idle_timeout(record)))
        candidates.append(("hub", self.host.config["backend.idleTimeout"]))
        for source, value in candidates:
            if value is None or value == "":
                continue
            try:
                record["idleTimeout"] = parse_duration(value)
            except ValueError:
                self.host.log(f"backend {record['id']}: ignoring the {source} idle timeout {value!r}")
                continue
            record["idleTimeoutSource"] = source
            return

    def manifest_idle_timeout(self, record: dict) -> Any:
        path = str(record.get("definitionPath") or "")
        if not path.startswith("plugin:"):
            return None
        from eks_harness.backends.registry import plugin_definitions
        from eks_harness.plugins import PluginError
        from eks_harness.plugins.project import ProjectConfigError

        try:
            items = plugin_definitions(self.host.plugin_host(), record.get("tree"))
        except (PluginError, ProjectConfigError, OSError):
            return None
        return next((item.get("idleTimeout") for item in items if item.get("path") == path), None)

    def wait_ready(self, backend_id: str, timeout: float, abort: Callable[[], bool] | None = None) -> str:
        deadline = time.time() + max(0.0, timeout)
        while True:
            record = self.records.get(backend_id)
            status = record.get("status") if record else "stopped"
            if status == "running" and record and not record.get("checking"):
                return "running"
            if status == "failed":
                return "failed"
            if record is None or (status == "stopped" and not record.get("leases")):
                return "stopped"
            if time.time() >= deadline or (abort is not None and abort()):
                return str(status)
            time.sleep(0.2)

    def find_lease(self, lease_id: str) -> tuple[dict, dict] | None:
        for record in list(self.records.values()):
            lease = (record.get("leases") or {}).get(lease_id)
            if lease is not None:
                return record, lease
        return None

    def require_lease(self, lease_id: str) -> tuple[dict, dict]:
        found = self.find_lease(lease_id)
        if found is None:
            raise BackendLeaseGone(lease_id)
        return found

    def renew(self, lease_id: str, ttl: Any = None) -> dict:
        record, lease = self.require_lease(lease_id)
        with self.host.lock:
            renew_lease(lease, self.lease_ttl(ttl) if ttl is not None else lease.get("ttl"), time.time())
            self.host.save()
        self.emit_lease("renewed", record, lease)
        return self.lease_response(record["id"], lease_id)

    def release(self, lease_id: str, reason: str = "released") -> dict | None:
        found = self.find_lease(lease_id)
        if found is None:
            return None
        record, lease = found
        self.drop_leases(record, [lease_id], reason)
        return self.lease_out(record, lease)

    def drop_leases(self, record: dict, lease_ids: list[str], reason: str) -> list[dict]:
        dropped: list[dict] = []
        with self.host.lock:
            leases = record.setdefault("leases", {})
            for lease_id in lease_ids:
                lease = leases.pop(lease_id, None)
                if lease is not None:
                    dropped.append(lease)
            if dropped and not leases:
                record["idleSince"] = time.time()
            self.host.save()
        for lease in dropped:
            self.emit_lease("released", record, lease, reason=reason)
        if dropped and not record.get("leases") and duration_seconds(record.get("idleTimeout")) == 0 \
                and record.get("status") in IDLE_STATES:
            self.host.spawn(self.stop_idle, record["id"])
        return dropped

    def stop_idle(self, backend_id: str) -> None:
        record = self.records.get(backend_id)
        if record is None or record.get("leases") or record.get("status") not in IDLE_STATES:
            return
        timeout = describe_duration(record.get("idleTimeout"))
        self.stop(backend_id, final=True, reason=f"idle: no lease for {timeout}")

    def release_owner(self, owner: str, reason: str) -> list[str]:
        released: list[str] = []
        for record in list(self.records.values()):
            ids = [lid for lid, lease in (record.get("leases") or {}).items()
                   if lease.get("owner") == owner and not lease.get("driverSid")]
            released += [lease["id"] for lease in self.drop_leases(record, ids, reason)]
        return released

    def link(self, sid: str, ref: str, *, tree: str | None = None, key: str | None = None,
             params: dict | None = None, owner: str | None = None, project: str | None = None,
             session: str | None = None, label: str | None = None) -> dict:
        target = self.resolve_ref(ref, tree=tree, key=key, params=params)
        current = self.link_of(sid)
        if current is not None and current[0]["id"] != self.target_id(target):
            self.drop_leases(current[0], [current[1]["id"]], "the driver lease moved to another backend")
        return self.acquire({**target, "driverSid": sid, "kind": "driver", "owner": owner, "project": project,
                             "session": session, "holder": label or f"driver {sid}"})

    def target_id(self, target: dict) -> str:
        if target.get("id"):
            return str(target["id"])
        tree = str(Path(target["tree"]).expanduser().resolve()) if target.get("tree") else None
        return self.backend_id(target["definition"], self.identity_of({**target, "tree": tree}))

    def resolve_ref(self, ref: str, *, tree: str | None, key: str | None, params: dict | None) -> dict:
        ref = (ref or "").strip()
        if not ref:
            raise BackendError("name a backend lease id, a backend id or a backend definition")
        record: dict | None = None
        if is_backend_lease_id(ref):
            record = self.require_lease(ref)[0]
        elif ref in self.records:
            record = self.records[ref]
        if record is not None:
            return {"id": record["id"], "definition": record["definition"], "tree": record.get("tree"),
                    "instance": record.get("instance"), "params": record.get("params") or {}}
        if "@" in ref:
            definition, identity = ref.split("@", 1)
            if identity.startswith("tree:") and "#" not in identity:
                return {"id": ref, "definition": definition, "tree": identity[len("tree:"):], "instance": identity}
            return {"id": ref, "definition": definition, "tree": tree, "instance": identity}
        return {"definition": ref, "tree": tree, "key": key, "params": params or {}}

    def link_of(self, sid: str) -> tuple[dict, dict] | None:
        for record in list(self.records.values()):
            for lease in (record.get("leases") or {}).values():
                if lease.get("driverSid") == sid:
                    return record, lease
        return None

    def unlink(self, sid: str, reason: str = "the driver lease ended") -> list[str]:
        dropped: list[str] = []
        for record in list(self.records.values()):
            ids = [lid for lid, lease in (record.get("leases") or {}).items() if lease.get("driverSid") == sid]
            if ids:
                dropped += [lease["id"] for lease in self.drop_leases(record, ids, reason)]
        return dropped

    def link_summary(self, sid: str) -> dict | None:
        found = self.link_of(sid)
        if found is None:
            return None
        record, lease = found
        info = record.get("info") or {}
        return {"id": record["id"], "lease": lease["id"], "status": record.get("status"),
                "generation": int(record.get("generation") or 0), "url": info.get("url") or "",
                "error": str(record.get("error") or "").splitlines()[0][:300] if record.get("error") else None}

    def hold(self, backend_id: str, seconds: float, hold_id: str | None = None) -> dict:
        record = self.records.get(backend_id)
        if record is None:
            raise BackendError(f"no backend {backend_id}")
        holder = hold_id or "manual"
        if seconds > 0:
            self.acquire({"id": backend_id, "definition": record["definition"], "tree": record.get("tree"),
                          "instance": record.get("instance"), "params": record.get("params") or {},
                          "kind": "hold", "holder": holder, "reuseHolder": True, "ttl": seconds})
        else:
            ids = [lid for lid, lease in (record.get("leases") or {}).items()
                   if lease.get("kind") == "hold" and (hold_id is None or lease.get("holder") == hold_id)]
            self.drop_leases(record, ids, "hold cleared")
        return self.public(self.records.get(backend_id) or record)

    def ensure(self, body: dict) -> dict:
        definition, instance = body.get("definition"), body.get("instance")
        if not definition or not instance:
            raise BackendError("definition and instance are required")
        hold = float(body.get("hold") if body.get("hold") is not None else 300)
        request = {"definition": definition, "instance": instance, "id": body.get("id"), "tree": body.get("tree"),
                   "restart": bool(body.get("restart")), "kind": "hold", "holder": body.get("holdId") or "ensure",
                   "reuseHolder": True, "ttl": hold, "noLease": hold <= 0}
        response = self.acquire(request)
        return response["backend"]

    def lease_out(self, record: dict, lease: dict, stamp: float | None = None) -> dict:
        stamp = stamp or time.time()
        expires = lease.get("expires")
        return {**lease, "backendId": record["id"], "definition": record.get("definition"),
                "expiresIn": round(expires - stamp, 1) if expires else None}

    def lease_response(self, backend_id: str, lease_id: str | None) -> dict:
        record = self.records.get(backend_id)
        if record is None:
            return {"status": "released", "lease": None, "backend": None, "error": "the backend was removed"}
        lease = (record.get("leases") or {}).get(lease_id) if lease_id else None
        status = record.get("status")
        if status == "running" and not record.get("checking"):
            state = "granted"
        elif status == "failed":
            state = "failed"
        elif lease_id and lease is None:
            state = "released"
        else:
            state = "starting"
        response = {"status": state, "lease": self.lease_out(record, lease) if lease else None,
                    "backend": self.public(record)}
        if state == "failed":
            response["error"] = str(record.get("error") or "the backend failed")
        return response

    def leases(self, backend_id: str | None = None, owner: str | None = None) -> list[dict]:
        stamp = time.time()
        out = []
        for record in list(self.records.values()):
            if backend_id and record["id"] != backend_id:
                continue
            for lease in sorted((record.get("leases") or {}).values(), key=lambda item: item.get("created") or 0):
                if owner and lease.get("owner") != owner:
                    continue
                out.append(self.lease_out(record, lease, stamp))
        return out

    def info_file(self, record: dict) -> Path:
        digest = hashlib.sha1(record["id"].encode()).hexdigest()[:16]
        return self.host.paths.state_dir / "backends" / f"{digest}.info.json"

    def store_info(self, record: dict, info: dict) -> None:
        full = normalize_info(info)
        path = self.info_file(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(full, handle)
        path.with_suffix(".tmp").replace(path)
        with self.host.lock:
            record["info"] = redact_info(full)
            self.host.save()

    def stored_info(self, record: dict) -> dict:
        try:
            return json.loads(self.info_file(record).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return record.get("info") or {}

    def full_info(self, record: dict) -> dict:
        return reveal_info(self.stored_info(record))

    def refresh_info(self, backend_id: str) -> dict | None:
        return None

    def info(self, backend_id: str, secrets: bool = False) -> dict:
        record = self.records.get(backend_id)
        if record is None:
            raise BackendError(f"no backend {backend_id}")
        return self.full_info(record) if secrets else redact_info(record.get("info") or {})

    def drop_info(self, record: dict) -> None:
        self.info_file(record).unlink(missing_ok=True)

    def mark_running(self, record: dict, info: dict, adopted: bool = False) -> None:
        self.store_info(record, info)
        with self.host.lock:
            previous = int(record.get("generation") or 0)
            if not adopted or not previous:
                record["generation"] = previous + 1
            record["status"] = "running"
            record["readyAt"] = time.time()
            record["verifiedAt"] = time.time()
            record.pop("error", None)
            if not record.get("leases"):
                record.setdefault("idleSince", time.time())
            self.host.save()
        if previous and not adopted:
            linked = [lease["driverSid"] for lease in (record.get("leases") or {}).values() if lease.get("driverSid")]
            self.host.emit(BACKEND_REPLACED_EVENT, resource=f"backend:{record['id']}",
                           detail={"backend": record["id"], "generation": record["generation"], "linked": linked,
                                   "url": (record.get("info") or {}).get("url")})

    def bindings(self, record: dict) -> list[str]:
        found = []
        for lease in sorted((record.get("leases") or {}).values(), key=lambda item: item.get("created") or 0):
            if lease.get("driverSid"):
                found.append(f"driver {lease['driverSid']}")
            elif lease.get("kind") == "hold":
                found.append(f"hold {lease.get('holder')}")
            else:
                found.append(f"lease {lease.get('holder') or lease['id']}")
        return found

    def tick(self) -> None:
        stamp = time.time()
        expired: list[tuple[dict, dict, str]] = []
        to_stop: list[str] = []
        with self.host.lock:
            for record in list(self.records.values()):
                leases = record.setdefault("leases", {})
                for lease_id, lease in list(leases.items()):
                    reason = None
                    if lease.get("expires") and float(lease["expires"]) < stamp:
                        reason = "expired: no renewal"
                    elif lease.get("driverSid") and not self.host.link_alive(str(lease["driverSid"])):
                        reason = "the driver lease ended"
                    if reason:
                        leases.pop(lease_id)
                        expired.append((record, lease, reason))
                if leases:
                    record.pop("idleSince", None)
                    continue
                if record.get("status") not in IDLE_STATES:
                    continue
                if "idleTimeout" not in record:
                    self.resolve_idle_timeout(record)
                record.setdefault("idleSince", stamp)
                seconds = duration_seconds(record.get("idleTimeout"))
                if seconds is not None and stamp - float(record["idleSince"]) >= seconds:
                    to_stop.append(record["id"])
            self.host.save()
        for record, lease, reason in expired:
            self.emit_lease("expired", record, lease, reason=reason)
        for backend_id in to_stop:
            self.stop_idle(backend_id)
        self.check_running()

    def instance_ended(self, instance: str) -> None:
        self.release_owner(instance, "harness instance ended")
        for record in list(self.records.values()):
            if record.get("instance") != instance:
                continue
            shared = [lease for lease in (record.get("leases") or {}).values() if lease.get("kind") != "hold"]
            if shared:
                self.host.log(f"backend {record['id']}: harness instance {instance} ended but {len(shared)} other "
                              f"lease(s) still hold it; keeping it")
                continue
            self.stop(record["id"], final=True, reason="harness instance ended")

    def stop_with_leases(self, backend_id: str, final: bool, reason: str) -> None:
        record = self.records.get(backend_id)
        if record is None:
            return
        self.drop_leases(record, list(record.get("leases") or {}), f"backend stopped: {reason}")
        with self.host.lock:
            record.pop("resume", None)
        self.stop(backend_id, final=final, reason=reason)

    def after_stop(self, backend_id: str) -> None:
        plan = None
        with self.host.lock:
            record = self.records.get(backend_id)
            if record is None:
                return
            resume = record.pop("resume", False)
            if resume and record.get("leases") and record.get("status") == "stopped":
                record["status"] = "preparing"
                record.pop("error", None)
                plan = "start"
            self.host.save()
        if plan:
            self.bring_up(backend_id, plan)

    def reconcile(self) -> list[str]:
        stamp = time.time()
        with self.host.lock:
            for record in self.records.values():
                record.pop("checking", None)
                self.upgrade_record(record, stamp)
                for lease in (record.get("leases") or {}).values():
                    if lease.get("expires"):
                        lease["expires"] = max(float(lease["expires"]), stamp + RESTART_GRACE)
            pending = [r["id"] for r in self.records.values() if r.pop("adopt", None)
                       and r.get("status") in TRANSITIONAL_BACKEND_STATES]
            running = [r["id"] for r in self.records.values() if r.get("status") == "running"]
            self.host.save()
        for backend_id in pending:
            self.host.log(f"backend {backend_id}: re-adopting it in state {self.records[backend_id]['status']}")
            self.host.spawn(self.adopt, backend_id)
        for backend_id in running:
            self.host.spawn(self.readopt, backend_id)
        self.host.spawn(self.sweep_orphans)
        return pending

    def upgrade_record(self, record: dict, stamp: float) -> None:
        leases = record.setdefault("leases", {})
        for holder, until in (record.pop("holds", None) or {}).items():
            if float(until) > stamp:
                lease = new_lease(record, {"kind": "hold", "holder": holder}, float(until) - stamp, stamp)
                leases[lease["id"]] = lease
        empty = record.pop("emptySince", None)
        if empty and not leases:
            record.setdefault("idleSince", float(empty))
        record.setdefault("generation", 1 if record.get("status") == "running" else 0)
        if "idleTimeout" not in record:
            self.resolve_idle_timeout(record)

    def public(self, record: dict) -> dict:
        stamp = time.time()
        out = {k: v for k, v in record.items() if k not in ("leases", "checking", "resume", "holds")}
        leases = [self.lease_out(record, lease, stamp)
                  for lease in sorted((record.get("leases") or {}).values(), key=lambda item: item.get("created") or 0)]
        out["leases"] = leases
        out["bindings"] = self.bindings(record)
        out["logFile"] = str(self.log_path(record))
        out["alive"] = {name: self.process_alive(record, name) for name in record.get("processes") or []}
        out["info"] = redact_info(record.get("info") or {}) if record.get("info") else None
        out["generation"] = int(record.get("generation") or 0)
        seconds = duration_seconds(record.get("idleTimeout"))
        out["stopsAt"] = (float(record["idleSince"]) + seconds
                          if record.get("idleSince") and seconds is not None and not leases else None)
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
