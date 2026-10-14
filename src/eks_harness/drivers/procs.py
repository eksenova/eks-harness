from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from eks_harness.paths import Paths

OWNER_ENV = "EHX_OWNER_PID"
IDLE_ENV = "EHX_IDLE_EXIT_MS"
KILL_GRACE = 3.0
WORKER_PATTERN = re.compile(r"(?:eks_harness/_workers|workers)/(?:web|mobile)-driver/src/server\.mjs")
HARNESS_PATTERN = re.compile(r"/\.harness/")
DEV_PATTERN = re.compile(r"\b(?:next(?:-server)?\s+dev|expo\s+start|react-native\s+start|metro\b|vite\b)")
ReapReason = Literal["owner_gone", "process_gone"]


def process_start(pid: int) -> str | None:
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        return stat.rsplit(")", 1)[-1].split()[19]
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.strip()
    return text or None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def same_process(pid: int, started: str | None) -> bool:
    if not pid_alive(pid):
        return False
    if started is None:
        return True
    return process_start(pid) == started


@dataclass(frozen=True, slots=True)
class Owner:
    pid: int
    started: str | None

    @classmethod
    def current(cls) -> Owner:
        pid = os.getpid()
        return cls(pid, process_start(pid))

    def alive(self) -> bool:
        return same_process(self.pid, self.started)


@dataclass(frozen=True, slots=True)
class ProcRecord:
    id: str
    kind: str
    pid: int
    pgid: int
    started: str | None
    owner_pid: int
    owner_started: str | None
    argv: list[str] = field(default_factory=list[str])
    tree: str | None = None
    install: str | None = None
    key: str | None = None
    created: float = 0.0

    @property
    def owner(self) -> Owner:
        return Owner(self.owner_pid, self.owner_started)

    def running(self) -> bool:
        return same_process(self.pid, self.started)


@dataclass(frozen=True, slots=True)
class Reaped:
    record: ProcRecord
    reason: ReapReason
    killed: bool


class ProcRegistry:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    @classmethod
    def for_paths(cls, paths: Paths) -> ProcRegistry:
        return cls(paths.state_dir / "procs")

    def _file(self, record_id: str) -> Path:
        return self.folder / f"{record_id}.json"

    def register(self, record: ProcRecord) -> ProcRecord:
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self._file(record.id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(record)), encoding="utf-8")
        os.replace(temporary, target)
        return record

    def remove(self, record_id: str) -> None:
        self._file(record_id).unlink(missing_ok=True)

    def records(self) -> list[ProcRecord]:
        if not self.folder.is_dir():
            return []
        found: list[ProcRecord] = []
        for file in sorted(self.folder.glob("*.json")):
            try:
                data: Mapping[str, Any] = json.loads(file.read_text(encoding="utf-8"))
                found.append(ProcRecord(**data))
            except (OSError, ValueError, TypeError):
                file.unlink(missing_ok=True)
        return found

    def find(self, kind: str, key: str) -> ProcRecord | None:
        for record in self.records():
            if record.kind == kind and record.key == key and record.running():
                return record
        return None


def kill_group(pid: int, pgid: int | None = None, grace: float = KILL_GRACE) -> bool:
    if not pid_alive(pid):
        return False
    group = pgid or pid
    killpg = getattr(os, "killpg", None)
    hard = getattr(signal, "SIGKILL", signal.SIGTERM)
    for sig in (signal.SIGTERM, hard):
        try:
            if killpg is None:
                raise OSError("no process groups on this platform")
            killpg(group, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except OSError:
                return True
        deadline = time.monotonic() + (grace if sig == signal.SIGTERM else 1.0)
        while time.monotonic() < deadline and pid_alive(pid):
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            time.sleep(0.05)
        if not pid_alive(pid):
            return True
    return not pid_alive(pid)


def spawn_owned(argv: Sequence[str], *, kind: str, registry: ProcRegistry, owner: Owner | None = None,
                cwd: str | Path | None = None, env: Mapping[str, str] | None = None,
                stdout: IO[bytes] | int | None = None, tree: str | None = None, install: str | None = None,
                key: str | None = None, idle_exit_ms: int | None = None) -> tuple[subprocess.Popen[bytes], ProcRecord]:
    chosen = owner or Owner.current()
    environment = dict(env if env is not None else os.environ)
    environment[OWNER_ENV] = str(chosen.pid)
    if idle_exit_ms:
        environment[IDLE_ENV] = str(idle_exit_ms)
    process = subprocess.Popen(list(argv), cwd=str(cwd) if cwd else None, env=environment, stdout=stdout,
                               stderr=subprocess.STDOUT if stdout is not None else None, stdin=subprocess.DEVNULL,
                               start_new_session=True)
    record = ProcRecord(id=uuid.uuid4().hex[:16], kind=kind, pid=process.pid, pgid=process.pid,
                        started=process_start(process.pid), owner_pid=chosen.pid, owner_started=chosen.started,
                        argv=list(argv), tree=tree, install=install, key=key, created=time.time())
    registry.register(record)
    return process, record


def stop_record(registry: ProcRegistry, record: ProcRecord) -> bool:
    killed = kill_group(record.pid, record.pgid) if record.running() else False
    registry.remove(record.id)
    return killed


def reap(registry: ProcRegistry) -> list[Reaped]:
    reaped: list[Reaped] = []
    for record in registry.records():
        if not record.running():
            registry.remove(record.id)
            reaped.append(Reaped(record, "process_gone", False))
            continue
        if record.owner.alive():
            continue
        reaped.append(Reaped(record, "owner_gone", stop_record(registry, record)))
    return reaped


def stop_owned_by(registry: ProcRegistry, owner: Owner) -> list[ProcRecord]:
    stopped: list[ProcRecord] = []
    for record in registry.records():
        if record.owner_pid == owner.pid and record.owner_started == owner.started:
            stop_record(registry, record)
            stopped.append(record)
    return stopped


@dataclass(frozen=True, slots=True)
class Orphan:
    pid: int
    started: str
    command: str
    kind: Literal["worker", "harness", "dev"]


def scan_orphans(lines: Iterable[str] | None = None, *, include_dev: bool = False) -> list[Orphan]:
    rows = list(lines) if lines is not None else _ps_rows()
    found: list[Orphan] = []
    for row in rows:
        parsed = _parse_ps(row)
        if parsed is None:
            continue
        pid, ppid, started, command = parsed
        if ppid != 1 or pid == os.getpid():
            continue
        if WORKER_PATTERN.search(command):
            found.append(Orphan(pid, started, command, "worker"))
        elif HARNESS_PATTERN.search(command) and DEV_PATTERN.search(command):
            found.append(Orphan(pid, started, command, "harness"))
        elif include_dev and DEV_PATTERN.search(command):
            found.append(Orphan(pid, started, command, "dev"))
    return found


_PS_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.*)$")


def _parse_ps(row: str) -> tuple[int, int, str, str] | None:
    match = _PS_ROW.match(row)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), match.group(3), match.group(4)


def _ps_rows() -> list[str]:
    try:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,command="], capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return done.stdout.splitlines()


__all__ = [
    "IDLE_ENV",
    "OWNER_ENV",
    "Orphan",
    "Owner",
    "ProcRecord",
    "ProcRegistry",
    "Reaped",
    "kill_group",
    "pid_alive",
    "process_start",
    "reap",
    "same_process",
    "scan_orphans",
    "spawn_owned",
    "stop_owned_by",
    "stop_record",
]
