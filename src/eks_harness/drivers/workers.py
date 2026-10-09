from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.drivers.client import WorkerClient, WorkerError
from eks_harness.paths import Paths

WORKERS_ENV = "EKS_HARNESS_WORKERS_DIR"
NODE_ENV = "EKS_HARNESS_NODE"
SCRIPTS = {"web": "web-driver/src/server.mjs", "mobile": "mobile-driver/src/server.mjs"}
KIT_FILES = ("kit/src/daemon-client.mjs", "kit/src/script-runner.mjs", "kit/src/devtools-link.mjs",
             "kit/src/config.mjs", "kit/src/extensions.mjs")
READY_TIMEOUT = 60.0


def workers_root() -> Path:
    override = os.environ.get(WORKERS_ENV)
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    packaged = here.parent.parent / "_workers"
    if (packaged / SCRIPTS["web"]).is_file():
        return packaged
    source = here.parents[3] / "workers"
    return source


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
    stable = {k: v for k, v in config.items() if k not in ("port", "stateDir")}
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
    def __init__(self, paths: Paths, *, root: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.paths = paths
        self.root = root or workers_root()
        self.env = env or {}

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
        if current and not restart and current.code == code and current.config_hash == wanted:
            if current.client().alive(kind):
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
        full = {**config, "port": 0, "stateDir": str(state_dir)}
        fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(full, handle)
        log_file = state_dir / "worker.log"
        env = {**os.environ, **self.env, "EHX_WORKER_CONFIG": str(config_file)}
        script = self.root / SCRIPTS[kind]
        if not script.is_file():
            raise WorkerError(f"the {kind} driver worker is missing at {script}")
        with open(log_file, "ab") as log:
            process = subprocess.Popen([node_binary(), str(script)], cwd=str(state_dir), env=env, stdout=log,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + timeout
        ready: dict[str, Any] | None = None
        while time.time() < deadline:
            if process.poll() is not None:
                raise WorkerError(f"the {kind} driver worker exited with {process.returncode}:\n{_tail(log_file)}")
            if ready_file.is_file():
                try:
                    ready = json.loads(ready_file.read_text(encoding="utf-8"))
                    if ready.get("pid") == process.pid:
                        break
                except ValueError:
                    pass
                ready = None
            time.sleep(0.05)
        if not ready:
            _kill(process.pid)
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
        stopped = _kill(int(data.get("pid") or 0))
        file.unlink(missing_ok=True)
        return stopped

    def stop_all(self) -> int:
        return sum(1 for handle in self.list() if self.stop(handle.kind, handle.key))


def _kill(pid: int, grace: float = 3.0) -> bool:
    if not pid_alive(pid):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    deadline = time.time() + grace
    while time.time() < deadline and pid_alive(pid):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    if pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    return True


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def encoder_command() -> list[str]:
    return [sys.executable, "-m", "eks_harness.capture.encode"]
