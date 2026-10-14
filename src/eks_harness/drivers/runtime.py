from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import JsonValue

from eks_harness.drivers.procs import (
    Owner,
    ProcRecord,
    ProcRegistry,
    spawn_owned,
    stop_record,
)
from eks_harness.engine.auth import persona_entry
from eks_harness.engine.model import BackendHandle, LeaseHandle, RunInfo
from eks_harness.ir.plan import SessionSpec
from eks_harness.ir.registry import Registry, implementation

log = logging.getLogger("eks_harness.drivers.runtime")

DEV_KIND = "dev"
READY_TIMEOUT = 180.0
READY_POLL = 0.25


class AppRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppRuntime:
    url: str | None = None
    api_url: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict[str, Any])
    env: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class PersonaLogin:
    persona: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    credentials: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])


class RuntimeWorker(Protocol):
    @property
    def url(self) -> str: ...

    def call(self, op: str, args: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class Spawned:
    record: ProcRecord
    url: str | None
    reused: bool

    @property
    def pid(self) -> int:
        return self.record.pid


class AppContext:
    def __init__(self, *, app: str, platform: str, tree: Path | None, root: Path | None, profile: Mapping[str, Any],
                 lease: LeaseHandle, session: SessionSpec | None, run: RunInfo, backend: BackendHandle | None,
                 state_dir: Path, procs: ProcRegistry, owner: Owner | None = None,
                 device: Mapping[str, Any] | None = None, native: Any = None) -> None:
        self.app = app
        self.platform = platform
        self.tree = tree
        self.root = root
        self.profile = profile
        self.lease = lease
        self.session = session
        self.run = run
        self.backend = backend
        self.state_dir = state_dir
        self.procs = procs
        self.owner = owner or Owner.current()
        self.device: Mapping[str, Any] = dict(device or {})
        self.native = native
        self.log = log

    @property
    def backend_url(self) -> str | None:
        return self.backend.url if self.backend is not None else None

    @property
    def backend_info(self) -> Mapping[str, JsonValue]:
        if self.backend is None:
            return {}
        return self.backend.secrets or self.backend.info

    def persona(self, key: str) -> dict[str, JsonValue]:
        credentials = self.backend_info.get("credentials")
        if not isinstance(credentials, Mapping):
            return {}
        entry = persona_entry(credentials, key)
        return dict(entry) if entry is not None else {}

    def spawn(self, argv: Sequence[str], *, key: str | None = None, cwd: str | Path | None = None,
              env: Mapping[str, str] | None = None, ready_url: str | None = None,
              ready_log: str | None = None, timeout: float = READY_TIMEOUT) -> Spawned:
        identity = key or hashlib.sha256("\0".join([*argv, str(cwd or "")]).encode()).hexdigest()[:16]
        existing = self.procs.find(DEV_KIND, identity)
        if existing is not None:
            return Spawned(existing, ready_url, True)
        logs = self.state_dir / "procs"
        logs.mkdir(parents=True, exist_ok=True)
        log_file = logs / f"{re.sub(r'[^A-Za-z0-9_.-]', '-', identity)}.log"
        environment = {**os.environ, **(env or {})}
        with open(log_file, "ab") as output:
            process, record = spawn_owned(list(argv), kind=DEV_KIND, registry=self.procs, owner=self.owner,
                                          cwd=cwd, env=environment, stdout=output,
                                          tree=str(self.tree) if self.tree else None, key=identity)
        try:
            _wait_ready(process.poll, ready_url, log_file, ready_log, timeout, argv)
        except BaseException:
            stop_record(self.procs, record)
            raise
        return Spawned(record, ready_url, False)


def _wait_ready(poll: Callable[[], int | None], url: str | None, log_file: Path, pattern: str | None,
                timeout: float, argv: Sequence[str]) -> None:
    if url is None and pattern is None:
        return
    compiled = re.compile(pattern) if pattern else None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = poll()
        if code is not None:
            raise AppRuntimeError(f"{argv[0]} exited with {code} before it was ready:\n{_tail(log_file)}")
        if compiled is not None and compiled.search(_tail(log_file, 200)):
            return
        if url is not None and _answers(url):
            return
        time.sleep(READY_POLL)
    raise AppRuntimeError(f"{' '.join(argv)} was not ready within {timeout:.0f} s:\n{_tail(log_file)}")


def _answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return int(response.status) < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def load_runtime(registry: Registry, app: str | None) -> object | None:
    if not app:
        return None
    spec = registry.app(app)
    if spec is None:
        return None
    found = implementation(spec.impl)
    if not callable(found):
        raise AppRuntimeError(f"app runtime {app}: {spec.impl} cannot be loaded")
    return found()


def call_hook(runtime: object | None, hook: str, *args: Any) -> Any:
    fn = getattr(runtime, hook, None) if runtime is not None else None
    if not callable(fn):
        return None
    return fn(*args)


def has_hook(runtime: object | None, hook: str) -> bool:
    return runtime is not None and callable(getattr(runtime, hook, None))


__all__ = [
    "DEV_KIND",
    "AppContext",
    "AppRuntime",
    "AppRuntimeError",
    "PersonaLogin",
    "RuntimeWorker",
    "Spawned",
    "call_hook",
    "has_hook",
    "load_runtime",
]
