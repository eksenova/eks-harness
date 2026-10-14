from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.drivers.client import WorkerClient, WorkerError
from eks_harness.drivers.procs import (
    Owner,
    ProcRegistry,
    kill_group,
    spawn_owned,
    stop_record,
)
from eks_harness.paths import Paths

WORKERS_ENV = "EKS_HARNESS_WORKERS_DIR"
NODE_ENV = "EKS_HARNESS_NODE"
SCRIPTS = {"web": "web-driver/src/server.mjs", "mobile": "mobile-driver/src/server.mjs"}
KIT_FILES = ("kit/src/daemon-client.mjs", "kit/src/script-runner.mjs", "kit/src/devtools-link.mjs",
             "kit/src/config.mjs", "kit/src/extensions.mjs", "kit/src/backend-link.mjs", "kit/src/ops.mjs",
             "kit/src/schema.mjs", "kit/src/targets.mjs", "kit/src/lifetime.mjs", "kit/src/icons.mjs",
             "kit/src/pace.mjs", "kit/src/boxmap.mjs", "web-driver/src/ops.mjs",
             "mobile-driver/src/ops.mjs")
UNHASHED_CONFIG = ("port", "stateDir", "backend")
READY_TIMEOUT = 60.0
IDLE_EXIT_MS = 30 * 60 * 1000
WORKER_KIND_PREFIX = "worker."


def hub_owner(paths: Paths) -> Owner:
    try:
        info = json.loads(paths.daemon_info_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = None
    if isinstance(info, dict) and isinstance(info.get("pid"), int):
        owner = Owner(int(info["pid"]), info.get("started") or None)
        if owner.alive():
            return owner
    return Owner.current()


def workers_root() -> Path:
    override = os.environ.get(WORKERS_ENV)
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    source = here.parents[3] / "workers"
    if (source / SCRIPTS["web"]).is_file():
        return source
    return here.parent.parent / "_workers"


def node_binary() -> str:
    found = os.environ.get(NODE_ENV) or shutil.which("node")
    if not found:
        raise WorkerError("node is not on PATH; the driver workers need Node.js 22 or newer")
    return found


def code_hash(kind: str, root: Path | None = None) -> str:
    root = root or workers_root()
    digest = hashlib.sha256()
    for rel in (SCRIPTS[kind], *KIT_FILES):
        path = root / rel
        digest.update(rel.encode())
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()[:16]


def config_hash(config: dict[str, Any]) -> str:
    stable = {k: v for k, v in config.items() if k not in UNHASHED_CONFIG}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class WorkerHandle:
    kind: str
    key: str
    pid: int
    port: int
    state_dir: Path
    log_file: Path
    code: str
    config_hash: str
    started_at: float
    reused: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> WorkerClient:
        return WorkerClient(self.base_url)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "pid": self.pid, "port": self.port, "url": self.base_url,
                "stateDir": str(self.state_dir), "log": str(self.log_file), "code": self.code,
                "configHash": self.config_hash, "startedAt": self.started_at, "reused": self.reused, **self.extra}


class WorkerManager:
    def __init__(self, paths: Paths, *, root: Path | None = None, env: dict[str, str] | None = None,
                 owner: Owner | None = None, idle_exit_ms: int = IDLE_EXIT_MS) -> None:
        self.paths = paths
        self.root = root or workers_root()
        self.env = env or {}
        self.owner = owner
        self.idle_exit_ms = idle_exit_ms
        self.procs = ProcRegistry.for_paths(paths)

    def state_dir(self, kind: str, key: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in key)[:80]
        return self.paths.state_dir / "workers" / f"{kind}-{safe}"

    def _record_file(self, kind: str, key: str) -> Path:
        return self.state_dir(kind, key) / "record.json"

    def get(self, kind: str, key: str) -> WorkerHandle | None:
        file = self._record_file(kind, key)
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        handle = WorkerHandle(kind=kind, key=key, pid=int(data["pid"]), port=int(data["port"]),
                              state_dir=Path(data["stateDir"]), log_file=Path(data["log"]), code=data["code"],
                              config_hash=data["configHash"], started_at=float(data["startedAt"]),
                              extra=data.get("extra") or {})
        return handle if pid_alive(handle.pid) else None

    def list(self) -> list[WorkerHandle]:
        base = self.paths.state_dir / "workers"
        found = []
        if base.is_dir():
            for folder in sorted(base.iterdir()):
                record = folder / "record.json"
                if not record.is_file():
                    continue
                try:
                    data = json.loads(record.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                handle = self.get(data["kind"], data["key"])
                if handle:
                    found.append(handle)
        return found

    def ensure(self, kind: str, key: str, config: dict[str, Any], *, restart: bool = False,
               timeout: float = READY_TIMEOUT) -> WorkerHandle:
        if kind not in SCRIPTS:
            raise WorkerError(f"unknown worker kind {kind}")
        code = code_hash(kind, self.root)
        wanted = config_hash(config)
        current = self.get(kind, key)
        if (current and not restart and current.code == code and current.config_hash == wanted
                and current.client().alive()):
            current.reused = True
            return current
        if current:
            self.stop(kind, key)
        return self._spawn(kind, key, config, code, wanted, timeout)

    def _spawn(self, kind: str, key: str, config: dict[str, Any], code: str, wanted: str,
               timeout: float) -> WorkerHandle:
        state_dir = self.state_dir(kind, key)
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        ready_file = state_dir / "worker.json"
        ready_file.unlink(missing_ok=True)
        config_file = state_dir / "config.json"
        full = {**config, "port": 0, "stateDir": str(state_dir), "codeHash": code}
        fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(full, handle)
        log_file = state_dir / "worker.log"
        env = {k: v for k, v in {**os.environ, **self.env}.items() if k != "EKS_HARNESS_RENDER_SLOT"}
        env["EHX_WORKER_CONFIG"] = str(config_file)
        script = self.root / SCRIPTS[kind]
        if not script.is_file():
            raise WorkerError(f"the {kind} driver worker is missing at {script}")
        owner = self.owner or hub_owner(self.paths)
        with open(log_file, "ab") as log:
            process, proc = spawn_owned([node_binary(), str(script)], kind=WORKER_KIND_PREFIX + kind,
                                        registry=self.procs, owner=owner, cwd=state_dir, env=env, stdout=log,
                                        install=str(self.root), key=key, idle_exit_ms=self.idle_exit_ms)
        deadline = time.time() + timeout
        ready: dict[str, Any] | None = None
        while time.time() < deadline:
            if process.poll() is not None:
                self.procs.remove(proc.id)
                raise WorkerError(f"the {kind} driver worker exited with {process.returncode}:\n{_tail(log_file)}")
            if ready_file.is_file():
                try:
                    loaded = json.loads(ready_file.read_text(encoding="utf-8"))
                except ValueError:
                    loaded = None
                if isinstance(loaded, dict) and loaded.get("pid") == process.pid:
                    ready = loaded
                    break
            time.sleep(0.05)
        if not ready:
            stop_record(self.procs, proc)
            raise WorkerError(f"the {kind} driver worker did not start within {timeout:.0f}s:\n{_tail(log_file)}")
        handle = WorkerHandle(kind=kind, key=key, pid=process.pid, port=int(ready["port"]), state_dir=state_dir,
                              log_file=log_file, code=code, config_hash=wanted, started_at=time.time(),
                              extra={k: v for k, v in ready.items() if k not in ("ready", "port", "pid")})
        record = {"kind": kind, "key": key, "pid": handle.pid, "port": handle.port, "stateDir": str(state_dir),
                  "log": str(log_file), "code": code, "configHash": wanted, "startedAt": handle.started_at,
                  "extra": handle.extra}
        self._record_file(kind, key).write_text(json.dumps(record), encoding="utf-8")
        return handle

    def stop(self, kind: str, key: str) -> bool:
        file = self._record_file(kind, key)
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        pid = int(data.get("pid") or 0)
        stopped = False
        for proc in self.procs.records():
            if proc.pid == pid:
                stopped = stop_record(self.procs, proc) or stopped
        stopped = kill_group(pid) or stopped
        file.unlink(missing_ok=True)
        return stopped

    def stop_all(self) -> int:
        return sum(1 for handle in self.list() if self.stop(handle.kind, handle.key))


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def encoder_command() -> list[str]:
    return [sys.executable, "-m", "eks_harness.capture.encode"]
