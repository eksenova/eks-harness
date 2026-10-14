from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from eks_harness.backends.eks_defs import (
    eks_definitions,
    eks_runner_command,
    is_eks_path,
)
from eks_harness.backends.registry import (
    is_plugin_path,
    plugin_definitions,
    runner_command,
)
from eks_harness.paths import Paths
from eks_harness.pools.backend_info import default_info, merge_info, resolve_template
from eks_harness.pools.base import BackendError, PoolHost, iso_now
from eks_harness.pools.base import BackendManager as BackendManagerBase
from eks_harness.service import source_checkout_src

WINDOWS = platform.system() == "Windows"
DEFINITION_NAME = re.compile(r"[A-Za-z0-9_.-]+")
ADOPTABLE_STEPS = ("prepare", "teardown")
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def repo_cache_root(paths: Paths) -> Path:
    root = paths.cache_dir / "repos"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(args: list[str], cwd: str | Path | None = None, timeout: float = 30) -> str:
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def pid_started(pid: int) -> str:
    if WINDOWS:
        return ""
    return run(["ps", "-o", "lstart=", "-p", str(pid)])


def alive(pid: int | None, started: str | None = None) -> bool:
    if not pid:
        return False
    if WINDOWS:
        out = run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"])
        return f'"{int(pid)}"' in out
    try:
        reaped, _ = os.waitpid(int(pid), os.WNOHANG)
        if reaped:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    line = run(["ps", "-o", "stat=", "-o", "lstart=", "-p", str(int(pid))])
    if not line:
        return not started
    state, _, lstart = line.strip().partition(" ")
    if state.startswith("Z"):
        return False
    if started and " ".join(lstart.split()) != " ".join(started.split()):
        return False
    return True


def _signal_target(pid: int, group: bool, sig: int) -> None:
    try:
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        if group:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass


def kill_group(pid: int | None, started: str | None, grace: float = 8.0) -> bool:
    if not pid or not alive(pid, started):
        return False
    if WINDOWS:
        run(["taskkill", "/T", "/PID", str(pid)], timeout=grace + 5)
        deadline = time.time() + grace
        while time.time() < deadline and alive(pid):
            time.sleep(0.2)
        if alive(pid):
            run(["taskkill", "/T", "/F", "/PID", str(pid)], timeout=15)
        return True
    try:
        group = os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError):
        return False
    _signal_target(pid, group, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    _signal_target(pid, group, signal.SIGKILL)
    return True


def process_table() -> dict[int, tuple[int, str]]:
    table: dict[int, tuple[int, str]] = {}
    if WINDOWS:
        return table
    for line in run(["ps", "-axo", "pid=,ppid=,command="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def descendant_pids(pid: int, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    children: dict[int, list[int]] = {}
    for child, (parent, _) in (table if table is not None else process_table()).items():
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found:
                found.append(child)
                pending.append(child)
    return found


def listening(port: int) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.3)
                sock.connect((host, port))
                return True
        except OSError:
            continue
    return False


def health_ok(url: str | None, expect: str | None = None, timeout: float = 3.0) -> bool | None:
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "eks-harness/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
            if response.status >= 500:
                return False
            return expect in body if expect else True
    except urllib.error.HTTPError as error:
        return error.code < 500
    except Exception:
        return False


def log_tail(path: Path | str, lines: int) -> str:
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])


def uv_binary() -> str:
    found = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    return found


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(str(path), threading.RLock())
    with thread_lock, open(path, "a+") as handle:
        if WINDOWS:
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class Entry:
    role: str
    tree: str
    port: int | None
    pid: int
    started: str
    command: list[str]
    cwd: str
    log: str
    created: str
    supervised: bool
    child_pid: int | None = None
    child_started: str | None = None
    health: str | None = None
    meta: dict | None = None
    instance: str | None = None

    @property
    def key(self) -> str:
        return entry_key(self.role, self.instance or self.tree)

    def is_alive(self) -> bool:
        return alive(self.pid, self.started)


def entry_key(role: str, instance: str | None) -> str:
    if not instance:
        return role
    digest = hashlib.sha1(instance.encode()).hexdigest()[:10]
    return f"{role}@{digest}"


def supervisor_env(environment: dict[str, str]) -> dict[str, str]:
    env = dict(environment)
    src = source_checkout_src()
    if src is not None:
        prior = [entry for entry in (env.get("PYTHONPATH") or "").split(os.pathsep) if entry]
        if str(src) not in prior:
            prior.insert(0, str(src))
        env["PYTHONPATH"] = os.pathsep.join(prior)
    python_path = env.get("PYTHONPATH")
    if python_path:
        root = Path.cwd()
        env["PYTHONPATH"] = os.pathsep.join(
            entry if Path(entry).is_absolute() else str(root / entry)
            for entry in python_path.split(os.pathsep))
    return env


class AlreadyRunning(BackendError):
    def __init__(self, entry: Entry) -> None:
        super().__init__(f"{entry.role} is already running (pid {entry.pid})")
        self.entry = entry


class PortTaken(BackendError):
    def __init__(self, port: int) -> None:
        super().__init__(f"port {port} is already in use; nothing was stopped")
        self.port = port


class ProcessRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def procs_dir(self) -> Path:
        path = self.root / "procs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs_dir(self) -> Path:
        path = self.root / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def locked(self):
        return file_lock(self.root / ".registry.lock")

    def entry_path(self, key: str) -> Path:
        return self.procs_dir / f"{key}.json"

    def load(self, key: str) -> Entry | None:
        path = self.entry_path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Entry(**data)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def load_role(self, role: str, instance: str | None) -> Entry | None:
        return self.load(entry_key(role, instance))

    def save(self, entry: Entry) -> None:
        path = self.entry_path(entry.key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(entry), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def drop(self, key: str) -> None:
        self.entry_path(key).unlink(missing_ok=True)

    def all(self) -> list[Entry]:
        entries = []
        for path in sorted(self.procs_dir.glob("*.json")):
            entry = self.load(path.stem)
            if entry:
                entries.append(entry)
        return entries

    def stop(self, entry: Entry) -> bool:
        tree: list[tuple[int, str]] = []
        if entry.child_pid:
            tree.append((entry.child_pid, entry.child_started or ""))
        if alive(entry.pid, entry.started):
            tree += [(pid, pid_started(pid)) for pid in descendant_pids(entry.pid) if pid != entry.child_pid]
        stopped = kill_group(entry.pid, entry.started, grace=12.0)
        for pid, started in tree:
            stopped = kill_group(pid, started or None, grace=4.0) or stopped
        self.drop(entry.key)
        return stopped

    def start(self, role: str, command: list[str], *, instance: str, tree: Path | None = None,
              port: int | None = None, cwd: Path | None = None, log: Path | None = None,
              env: dict[str, str] | None = None, base_env: dict[str, str] | None = None, supervise: bool = False,
              health: str | None = None, health_expect: str | None = None, health_grace: float = 90,
              meta: dict | None = None) -> Entry:
        key = entry_key(role, instance)
        work_dir = Path(cwd or tree or Path.cwd()).resolve()
        log_path = Path(log).expanduser().resolve() if log else self.logs_dir / f"{key}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.locked():
            existing = self.load(key)
            if existing and existing.is_alive():
                raise AlreadyRunning(existing)
            if existing:
                self.drop(key)
            if port and listening(port):
                raise PortTaken(port)
            environment = dict(base_env if base_env is not None else os.environ)
            environment.setdefault("LANG", "en_US.UTF-8")
            environment.setdefault("LC_ALL", "en_US.UTF-8")
            environment.update(env or {})
            if supervise:
                environment = supervisor_env(environment)
                launch = [sys.executable, "-m", "eks_harness.pools.backends", "_supervise",
                          "--procs-dir", str(self.procs_dir), "--key", key, "--log", str(log_path),
                          "--health-grace", str(health_grace)]
                if health:
                    launch += ["--health", health]
                if health_expect:
                    launch += ["--health-expect", health_expect]
                launch += ["--", *command]
            else:
                launch = list(command)
            with open(log_path, "ab", buffering=0) as handle:
                handle.write(f"\n==== {iso_now()} start {role} {' '.join(command)}\n".encode())
                process = subprocess.Popen(launch, cwd=work_dir, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=handle, stderr=subprocess.STDOUT, close_fds=True,
                                           **detached_kwargs())
            time.sleep(0.05)
            entry = Entry(role=role, tree=str(tree) if tree else "", port=port, pid=process.pid,
                          started=pid_started(process.pid), command=list(command), cwd=str(work_dir),
                          log=str(log_path), created=iso_now(), supervised=supervise, health=health, meta=meta,
                          instance=instance)
            self.save(entry)
        return entry


def detached_kwargs() -> dict:
    if WINDOWS:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


class InstanceRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def directory(self) -> Path:
        return self.root / "instances"

    def available(self) -> bool:
        return self.directory.is_dir()

    def read(self) -> dict[int, dict]:
        found: dict[int, dict] = {}
        if not self.directory.is_dir():
            return found
        for path in self.directory.glob("slot-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data.get("slot"), int):
                found[data["slot"]] = data
        return found

    def find(self, key: str) -> dict | None:
        return next((data for data in self.read().values() if data.get("key") == key), None)

    def reserved_ports(self) -> set[int]:
        ports: set[int] = set()
        for data in self.read().values():
            for value in (data.get("ports") or {}).values():
                try:
                    ports.add(int(value))
                except (TypeError, ValueError):
                    continue
        return ports


SECRET_STEPS = ("start", "info")
SEED_HOOKS = ("reset", "seed_phase", "snapshot", "restore", "migrations", "drop_snapshot")
LEGACY_STEPS = frozenset({"describe", "fingerprint", "prepare", "spec", "teardown", "seed", "snapshot", "restore"})
READY_TIMEOUT = 300.0


class BackendManager(BackendManagerBase):
    def __init__(self, host: PoolHost) -> None:
        super().__init__(host)
        self.repo_root = repo_cache_root(host.paths)
        self.registry = ProcessRegistry(self.repo_root)
        self.instances = InstanceRegistry(self.repo_root)
        self.clashes: set[tuple[str, str | None]] = set()

    def emit(self, record: dict, **detail) -> None:
        self.host.emit("backend.status", resource=f"backend:{record['id']}",
                       detail={"status": record.get("status"), "definition": record.get("definition"),
                               "instance": record.get("instance"), "generation": record.get("generation"),
                               **detail})

    def definitions(self, tree: str | None = None) -> list[dict]:
        found: list[dict] = []
        seen: set[str] = set()
        for source, folder in self.definition_dirs(tree):
            if not folder.is_dir():
                continue
            for entry in sorted(folder.iterdir()):
                if not entry.is_file() or entry.name.startswith((".", "_")):
                    continue
                if entry.suffix not in (".py", "") and not os.access(entry, os.X_OK):
                    continue
                name = entry.stem if entry.suffix == ".py" else entry.name
                if name in seen or not DEFINITION_NAME.fullmatch(name):
                    continue
                seen.add(name)
                found.append({"name": name, "path": str(entry), "source": source, "tree": tree})
        for item in plugin_definitions(self.host.plugin_host(), tree):
            if item["name"] not in seen:
                seen.add(item["name"])
                found.append(item)
        for item in eks_definitions(tree):
            if item["name"] in seen:
                self.report_clash(item["name"], tree)
                continue
            seen.add(item["name"])
            found.append(item)
        return found

    def report_clash(self, name: str, tree: str | None) -> None:
        key = (name, tree)
        if key in self.clashes:
            return
        self.clashes.add(key)
        self.host.log(f"backend definition {name}: an @eks.backend of {tree} has the name of a script or plugin "
                      "definition, which wins")

    def find_definition(self, name: str, tree: str | None) -> Path:
        if not name or not DEFINITION_NAME.fullmatch(name):
            raise BackendError(f"invalid backend definition name: {name}")
        places = self.definition_dirs(tree)
        for _, folder in places:
            for candidate in (folder / f"{name}.py", folder / name):
                if candidate.is_file():
                    return candidate
        for item in plugin_definitions(self.host.plugin_host(), tree):
            if item["name"] == name:
                return Path(item["path"])
        for item in eks_definitions(tree):
            if item["name"] == name:
                return Path(item["path"])
        raise BackendError(f"no backend definition '{name}' in {', '.join(str(p) for _, p in places)}")

    def state_dir(self, record: dict) -> Path:
        instance = self.instances.find(record["instance"])
        if instance:
            path = self.repo_root / "state" / f"slot-{instance['slot']}" / f"backend-{record['definition']}"
        else:
            digest = hashlib.sha1(record["id"].encode()).hexdigest()[:10]
            path = self.repo_root / "backends" / f"{record['definition']}-{digest}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(self, record: dict) -> Path:
        return self.state_dir(record) / "backend.log"

    def context(self, record: dict) -> dict:
        instance = self.instances.find(record["instance"]) or {}
        return {
            "backendId": record["id"], "definition": record["definition"], "instance": record["instance"],
            "key": record.get("key") or record["instance"], "params": record.get("params") or {},
            "tree": record.get("tree"), "slot": instance.get("slot"), "stateDir": str(self.state_dir(record)),
            "ports": record.get("ports", {}), "captured": record.get("captured") or {},
            "exports": record.get("exports") or {}, "generation": int(record.get("generation") or 0),
            "daemonUrl": self.host.url, "dependencies": self.dependency_context(record),
        }

    def command_for(self, script: Path) -> list[str]:
        if is_plugin_path(script):
            return runner_command(script)
        if is_eks_path(script):
            return eks_runner_command(script)
        if script.suffix == ".py":
            return [uv_binary(), "run", "--quiet", "--script", str(script)]
        return [str(script)]

    def implements(self, record: dict, step: str) -> bool:
        steps = record.get("implements")
        return step in (set(steps) if isinstance(steps, list) else LEGACY_STEPS)

    def run(self, record: dict, command: str, *args: str, timeout: float = 120, stream: bool = False) -> str:
        script = Path(record["definitionPath"])
        context_file = self.state_dir(record) / "context.json"
        context_file.write_text(json.dumps(self.context(record), ensure_ascii=False, indent=2), encoding="utf-8")
        env = dict(os.environ)
        env.update({"EKS_TREE": record.get("tree") or "", "EKS_HARNESS_INSTANCE": record["instance"],
                    "EKS_BACKEND_ID": record["id"], "EKS_BACKEND_CONTEXT": str(context_file)})
        argv = [*self.command_for(script), command, "--context", str(context_file), *args]
        log_file = self.log_path(record)
        with open(log_file, "ab", buffering=0) as log:
            log.write(f"\n==== {iso_now()} {record['definition']} {command} {' '.join(args)}\n".encode())
            try:
                process = subprocess.Popen(argv, cwd=record.get("tree") or None, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log if stream else subprocess.PIPE, stderr=log)
            except OSError as error:
                raise BackendError(f"{command} could not run {argv[0]}: {error}") from error
            if command in ADOPTABLE_STEPS:
                with self.host.lock:
                    record["step"] = {"command": command, "pid": process.pid, "started": pid_started(process.pid),
                                      "at": time.time(), "timeout": timeout}
                    self.host.save()
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.communicate()
                raise BackendError(f"{command} did not finish in {timeout:.0f}s; log: {log_file}") from error
            finally:
                if command in ADOPTABLE_STEPS:
                    with self.host.lock:
                        record.pop("step", None)
                        self.host.save()
        if process.returncode != 0:
            raise BackendError(f"{command} failed (exit {process.returncode}); log: {log_file}\n"
                               + log_tail(log_file, 25))
        if stream:
            return ""
        return stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else ""

    def wait_step(self, record: dict) -> int | None:
        step = record.get("step") or {}
        pid = step.get("pid")
        if not pid:
            return None
        deadline = float(step.get("at") or time.time()) + float(step.get("timeout") or 3600)
        self.host.log(f"backend {record['id']}: waiting for the {step.get('command')} step it was running "
                      f"(pid {pid}) before the restart")
        code: int | None = None
        while True:
            try:
                reaped, status = os.waitpid(int(pid), os.WNOHANG)
            except ChildProcessError:
                reaped, status = 0, None
            except OSError:
                reaped, status = 0, None
            if reaped:
                code = os.waitstatus_to_exitcode(status)
                break
            if status is None and not alive(pid, step.get("started")):
                break
            if time.time() > deadline:
                self.host.log(f"backend {record['id']}: the {step.get('command')} step outlived its timeout; "
                              f"stopping it")
                kill_group(pid, step.get("started"), grace=10)
                code = -1
                break
            time.sleep(1.0)
        with self.host.lock:
            record.pop("step", None)
            self.host.save()
        return code

    def adopt(self, backend_id: str) -> None:
        record = self.records.get(backend_id)
        if record is None:
            return
        status = record.get("status")
        step = dict(record.get("step") or {})
        code = self.wait_step(record)
        if status == "stopping":
            self.finish_stop(record, bool(record.get("stopFinal")), str(record.get("stopReason") or "restart"),
                             skip_teardown=step.get("command") in ("teardown", "stop", "cleanup") and code == 0)
            return
        if status == "preparing":
            resume = "spec" if step.get("command") == "prepare" and code == 0 else None
            self.start(backend_id, resume=resume)
            return
        if status == "starting":
            self.start(backend_id, resume="launch")

    def readopt(self, backend_id: str) -> None:
        record = self.records.get(backend_id)
        if record is None or record.get("status") != "running":
            return
        reason = self.down_reason(record)
        if reason is None:
            self.host.log(f"backend {backend_id}: still running after the restart; re-adopted with "
                          f"{len(record.get('leases') or {})} lease(s)")
            self.emit(record, adopted=True)
            return
        if record.get("leases"):
            self.host.log(f"backend {backend_id}: {reason} while the daemon was down; starting it again")
            self.replace(backend_id, reason)
        else:
            self.host.log(f"backend {backend_id}: {reason} while the daemon was down and nothing holds it; "
                          f"cleaning it up")
            self.stop(backend_id, final=True, reason=f"orphaned: {reason}")

    def sweep_orphans(self) -> list[str]:
        known = set(self.records)
        swept: dict[str, tuple[str, Entry]] = {}
        for entry in self.registry.all():
            owner = (entry.meta or {}).get("backend")
            if not owner or owner in known:
                continue
            self.host.log(f"backend {owner}: process {entry.role} (pid {entry.pid}) has no backend record; "
                          f"stopping it")
            self.registry.stop(entry)
            swept.setdefault(owner, (str((entry.meta or {}).get("definition") or ""), entry))
        for owner, (definition, entry) in swept.items():
            if definition:
                self.cleanup_orphan(owner, definition, entry)
        return list(swept)

    def cleanup_orphan(self, backend_id: str, definition: str, entry: Entry) -> None:
        identity = backend_id.split("@", 1)[1] if "@" in backend_id else entry.instance or ""
        tree = entry.tree or None
        try:
            path = self.find_definition(definition, tree)
        except BackendError as error:
            self.host.log(f"backend {backend_id}: cannot clean up the orphan: {error}")
            return
        record = {"id": backend_id, "definition": definition, "instance": identity, "tree": tree,
                  "definitionPath": str(path), "status": "stopping", "leases": {}}
        try:
            described = self.json_of(record, "describe")
            record["implements"] = described.get("implements")
        except BackendError:
            pass
        self.run_cleanup(record, final=True)

    def step(self, backend_id: str, step: str, *, name: str | None = None, phase: str | None = None,
             timeout: float = 3600) -> dict:
        record = self.records.get(backend_id)
        if record is None:
            raise BackendError(f"no backend {backend_id}")
        if step not in SEED_HOOKS:
            raise BackendError(f"{step} is not a seed hook ({', '.join(SEED_HOOKS)})")
        if not self.implements(record, step):
            return {"missing": True}
        args: list[str] = []
        if name is not None:
            args += ["--name", name]
        if phase is not None:
            args += ["--phase", phase]
        return self.json_of(record, step, *args, timeout=timeout)

    def json_of(self, record: dict, command: str, *args: str, timeout: float = 120) -> dict:
        out = self.run(record, command, *args, timeout=timeout).strip()
        shown = "" if command in SECRET_STEPS else f": {out[-300:]}"
        try:
            value = json.loads(out.splitlines()[-1] if out else "{}")
        except (json.JSONDecodeError, IndexError) as error:
            raise BackendError(f"{command} did not print JSON on its last line{shown}") from error
        if not isinstance(value, dict):
            raise BackendError(f"{command} printed JSON that is not an object{shown}")
        return value

    def owned(self, record: dict, port: int) -> bool:
        for name in record.get("processes") or []:
            entry = self.registry.load_role(name, record["instance"])
            if entry and entry.port == port and entry.is_alive():
                return True
        return False

    def allocate_ports(self, record: dict, names: list[str]) -> dict:
        ports = dict(record.get("ports") or {})
        reserved = {int(p) for other in self.records.values() if other["id"] != record["id"]
                    for p in (other.get("ports") or {}).values()}
        reserved |= self.instances.reserved_ports()
        start, end = int(self.host.config["backend.portRangeStart"]), int(self.host.config["backend.portRangeEnd"])
        mine = set(ports.values())
        for name in names:
            current = ports.get(name)
            if current and (not listening(current) or self.owned(record, current)):
                continue
            for candidate in range(start, end + 1):
                if candidate in reserved or candidate in mine or listening(candidate):
                    continue
                ports[name] = candidate
                mine.add(candidate)
                break
            else:
                raise BackendError(f"no free port in {start}-{end} for {name}")
        return ports

    def process_alive(self, record: dict, name: str) -> bool:
        entry = self.registry.load_role(name, record["instance"])
        return bool(entry and entry.is_alive())

    def healthy(self, record: dict) -> bool:
        if not self.implements(record, "health"):
            return True
        try:
            return bool(self.json_of(record, "health", timeout=60).get("healthy", True))
        except BackendError:
            return False

    def down_reason(self, record: dict) -> str | None:
        dead = [name for name in record.get("processes") or [] if not self.process_alive(record, name)]
        if dead:
            return f"process {', '.join(dead)} exited"
        if not self.healthy(record):
            return "its health check failed"
        return None

    def bring_up(self, backend_id: str, plan: str) -> None:
        if plan == "start":
            self.emit(self.records[backend_id])
            self.host.spawn(self.start, backend_id)
        elif plan == "restart":
            self.replace(backend_id, "restart requested")
        elif plan == "verify":
            self.verify(backend_id)

    def verify(self, backend_id: str) -> None:
        record = self.records.get(backend_id)
        if record is None:
            return
        reason: str | None = None
        try:
            reason = self.fingerprint_change(record) or self.down_reason(record)
        finally:
            with self.host.lock:
                record.pop("checking", None)
                record["verifiedAt"] = time.time()
                self.host.save()
        if reason:
            self.host.log(f"backend {backend_id}: {reason}; replacing it")
            self.replace(backend_id, reason)

    def current_fingerprint(self, record: dict) -> Any:
        if not self.implements(record, "fingerprint"):
            return None
        try:
            value = self.json_of(record, "fingerprint").get("fingerprint")
        except BackendError as error:
            self.host.log(f"backend {record['id']}: the fingerprint hook failed: {error}")
            return None
        return value or None

    def fingerprint_change(self, record: dict) -> str | None:
        recorded = record.get("fingerprint")
        first = self.current_fingerprint(record)
        if first is not None and recorded is None:
            with self.host.lock:
                record["fingerprint"] = first
                self.host.save()
        if first is None or recorded is None or first == recorded:
            return None
        second = self.current_fingerprint(record)
        if second != first:
            self.host.log(f"backend {record['id']}: its fingerprint is not stable ({first} then {second}); "
                          f"keeping the running instance")
            return None
        return "its fingerprint changed (new code or configuration)"

    def check_running(self) -> None:
        for record in list(self.records.values()):
            if record.get("status") != "running" or not record.get("leases") or record.get("checking"):
                continue
            dead = [name for name in record.get("processes") or [] if not self.process_alive(record, name)]
            if dead:
                self.host.log(f"backend {record['id']}: process {', '.join(dead)} is gone; replacing the backend")
                self.replace(record["id"], f"process {', '.join(dead)} exited")

    def replace(self, backend_id: str, reason: str) -> None:
        with self.host.lock:
            record = self.records.get(backend_id)
            if record is None or record.get("status") not in ("running", "failed"):
                return
            record["status"] = "stopping"
            record["stopFinal"] = False
            record["stopReason"] = reason
            record["resume"] = True
            self.host.save()
        self.emit(record, reason=reason, replacing=True)
        self.host.spawn(self.finish_stop, record, False, reason)

    def start(self, backend_id: str, resume: str | None = None) -> None:
        record = self.records.get(backend_id)
        if record is None:
            return
        try:
            info: dict = {}
            if resume is None:
                described = self.json_of(record, "describe")
                with self.host.lock:
                    record["description"] = described.get("description", "")
                    record["implements"] = described.get("implements") if isinstance(
                        described.get("implements"), list) else None
                    record["ports"] = self.allocate_ports(record, [str(p) for p in described.get("ports") or []])
                    self.resolve_idle_timeout(record, described)
                    self.host.save()
                self.acquire_dependencies(record)
                if self.implements(record, "prepare"):
                    self.host.log(f"backend {backend_id}: prepare")
                    self.run(record, "prepare", timeout=3600, stream=True)
            with self.host.lock:
                record["status"] = "starting"
                self.host.save()
            self.emit(record)
            if self.implements(record, "spec"):
                spec = self.json_of(record, "spec")
                self.launch(record, spec, adopt=resume == "launch")
                info = merge_info(resolve_template(spec.get("info") or {}, record.get("ports") or {},
                                                   record.get("captured") or {}))
            elif self.implements(record, "start"):
                if resume != "launch":
                    info = self.json_of(record, "start", timeout=3600)
                else:
                    info = dict(self.full_info(record))
                with self.host.lock:
                    record["processes"] = []
                    self.host.save()
            else:
                raise BackendError("the definition implements neither spec nor start")
            self.wait_healthy(record)
            if self.implements(record, "info"):
                info = merge_info(info, self.json_of(record, "info"))
            full = merge_info(default_info(record.get("ports") or {}, record.get("exports") or {},
                                           record.get("tree")), info)
            fingerprint = self.current_fingerprint(record)
            with self.host.lock:
                if fingerprint is not None:
                    record["fingerprint"] = fingerprint
                record["startedAt"] = record.get("startedAt") if resume == "launch" and record.get("startedAt") \
                    else time.time()
                self.host.save()
            self.mark_running(record, full, adopted=resume == "launch")
            self.write_exports(record)
            self.host.log(f"backend {backend_id}: running "
                          f"({', '.join(f'{k}={v}' for k, v in (record.get('ports') or {}).items())}), generation "
                          f"{record.get('generation')}" + (" (re-adopted after a restart)" if resume else ""))
            self.emit(record, ports=record.get("ports") or {})
        except Exception as error:
            self.host.log(f"backend {backend_id} failed: {str(error).splitlines()[0] if str(error) else error!r}")
            for name in record.get("processes") or []:
                entry = self.registry.load_role(name, record["instance"])
                if entry:
                    self.registry.stop(entry)
            with self.host.lock:
                record["status"] = "failed"
                record["error"] = str(error)[:2000]
                if not record.get("leases"):
                    record.setdefault("idleSince", time.time())
                self.host.save()
            self.emit(record, error=str(error)[:500])

    def refresh_info(self, backend_id: str) -> dict | None:
        record = self.records.get(backend_id)
        if record is None or record.get("status") != "running" or not self.implements(record, "info"):
            return None
        fresh = self.json_of(record, "info")
        self.store_info(record, merge_info(self.stored_info(record), fresh))
        self.emit(record, infoRefreshed=True)
        return record.get("info")

    def wait_healthy(self, record: dict) -> None:
        if not self.implements(record, "health"):
            return
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if self.healthy(record):
                return
            time.sleep(1.0)
        raise BackendError(f"the health check of {record['id']} did not pass in {READY_TIMEOUT:.0f}s")

    def launch(self, record: dict, spec: dict, adopt: bool = False) -> None:
        workdir = Path(spec.get("workdir") or record.get("tree") or self.state_dir(record))
        if not adopt:
            for item in spec.get("files") or []:
                path = Path(item["path"])
                path = path if path.is_absolute() else workdir / path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item.get("content", ""), encoding="utf-8")
                if item.get("mode"):
                    path.chmod(int(str(item["mode"]), 8))
        base = dict(os.environ) if spec.get("inheritEnv", True) else {}
        base.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
        captured: dict[str, str] = {}
        names: list[str] = []
        offsets = dict(record.get("logOffsets") or {}) if adopt else {}
        for process in spec.get("processes") or []:
            name = process["name"]
            names.append(name)
            existing = self.registry.load_role(name, record["instance"])
            if adopt and existing and existing.is_alive() and name in offsets:
                self.host.log(f"backend {record['id']}: process {name} is still running (pid {existing.pid}); "
                              f"re-adopted")
                with self.host.lock:
                    record["processes"] = list(names)
                    self.host.save()
                self.wait_process_ready(record, name, existing, process, captured, int(offsets[name]))
                continue
            if existing:
                self.registry.stop(existing)
            env = dict(base)
            env.update({k: str(v) for k, v in (process.get("env") or {}).items()})
            port = record["ports"].get(process.get("port")) if process.get("port") else None
            health = (process.get("health") or {}).get("url")
            planned_log = self.registry.logs_dir / f"{entry_key(name, record['instance'])}.log"
            offset = planned_log.stat().st_size if planned_log.exists() else 0
            offsets[name] = offset
            with self.host.lock:
                record["processes"] = list(names)
                record["logOffsets"] = dict(offsets)
                self.host.save()
            entry = self.registry.start(
                name, [str(c) for c in process["command"]], instance=record["instance"],
                tree=Path(record["tree"]) if record.get("tree") else None, cwd=Path(process.get("cwd") or workdir),
                port=port, base_env=env, env={}, supervise=True, health=health,
                health_grace=float((process.get("health") or {}).get("grace", 120)),
                meta={"backend": record["id"], "definition": record["definition"]})
            self.wait_process_ready(record, name, entry, process, captured, offset)
        with self.host.lock:
            record["captured"] = captured
            record["exports"] = self.resolve_exports(spec.get("exports") or {}, record, captured)
            logs = {}
            for name in names:
                entry = self.registry.load_role(name, record["instance"])
                if entry:
                    logs[name] = entry.log
            record["logs"] = logs
            self.host.save()

    def wait_process_ready(self, record: dict, name: str, entry: Entry, process: dict, captured: dict,
                   offset: int = 0) -> None:
        ready, health = process.get("ready") or {}, process.get("health") or {}
        timeout = float(ready.get("timeout") or health.get("timeout") or 120)
        deadline = time.time() + timeout
        log = Path(entry.log)
        pattern = re.compile(ready["pattern"]) if ready.get("pattern") else None
        matched = pattern is None
        healthy = not health.get("url")
        while time.time() < deadline:
            current = self.registry.load_role(name, record["instance"])
            if not current or not current.is_alive():
                raise BackendError(f"process {name} exited while starting; last output:\n{log_tail(log, 30)}")
            if not matched and pattern is not None and log.exists():
                with open(log, "rb") as handle:
                    handle.seek(offset)
                    text = handle.read().decode("utf-8", "replace")
                text = "\n".join(line for line in text.splitlines()
                                 if not line.startswith(("==== ", "[supervisor ")))
                found = pattern.search(text)
                if found:
                    matched = True
                    captured.update({k: v for k, v in found.groupdict().items() if v is not None})
            if not healthy:
                healthy = bool(health_ok(health["url"], health.get("expect")))
            if matched and healthy:
                return
            time.sleep(0.5)
        raise BackendError(f"process {name} not ready in {timeout:.0f}s; last output:\n{log_tail(log, 30)}")

    def resolve_exports(self, exports: dict, record: dict, captured: dict) -> dict:
        return {key: str(resolve_template(str(template), record.get("ports") or {}, captured))
                for key, template in exports.items()}

    def write_exports(self, record: dict) -> None:
        exports = dict(record.get("exports") or {})
        exports["EKS_BACKEND_ID"] = record["id"]
        lines = [f"{k}={json.dumps(v)}" for k, v in exports.items()]
        (self.state_dir(record) / "exports.env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def stop(self, backend_id: str, final: bool = False, reason: str = "") -> None:
        record = self.records.get(backend_id)
        if not record:
            return
        with self.host.lock:
            if record.get("status") == "stopping":
                return
            record["status"] = "stopping"
            record["stopFinal"] = final
            record["stopReason"] = reason
            self.host.save()
        self.emit(record, reason=reason)
        self.host.log(f"backend {backend_id}: stopping ({reason}){' with cleanup' if final else ''}")
        self.finish_stop(record, final, reason)

    def run_cleanup(self, record: dict, final: bool) -> None:
        if not record.get("definitionPath") or not (is_plugin_path(record["definitionPath"])
                                                   or is_eks_path(record["definitionPath"])
                                                   or Path(record["definitionPath"]).is_file()):
            return
        steps: list[tuple[str, list[str], float]] = []
        if self.implements(record, "stop"):
            steps.append(("stop", [], 600))
        elif self.implements(record, "teardown") and not final:
            steps.append(("teardown", [], 600))
        if final:
            if self.implements(record, "cleanup"):
                steps.append(("cleanup", [], 1800))
            elif self.implements(record, "teardown"):
                steps.append(("teardown", ["--final"], 1800))
        for command, args, timeout in steps:
            try:
                self.run(record, command, *args, timeout=timeout, stream=True)
            except Exception as error:
                self.host.log(f"backend {record['id']}: {command} failed: "
                              f"{str(error).splitlines()[0] if str(error) else error!r}")

    def finish_stop(self, record: dict, final: bool, reason: str, skip_teardown: bool = False) -> None:
        backend_id = record["id"]
        for name in reversed(record.get("processes") or []):
            entry = self.registry.load_role(name, record["instance"])
            if entry:
                self.registry.stop(entry)
        if not skip_teardown:
            self.run_cleanup(record, final)
        if final and not (bool(record.get("resume")) and bool(record.get("leases"))):
            self.release_dependencies(record, reason)
        with self.host.lock:
            keep = bool(record.get("resume")) and bool(record.get("leases"))
            record["status"] = "stopped"
            record["stoppedAt"] = time.time()
            record.pop("stopFinal", None)
            record.pop("stopReason", None)
            record.pop("logOffsets", None)
            if final:
                record.pop("info", None)
                if not keep:
                    self.records.pop(backend_id, None)
            elif not record.get("leases"):
                record.setdefault("idleSince", time.time())
            self.host.save()
        if final:
            self.drop_info(record)
        self.emit(record, reason=reason, final=final)
        self.after_stop(backend_id)

    def log_resources(self, record: dict) -> dict[str, str]:
        found = {f"backend:{record['id']}": str(self.log_path(record))}
        for name, path in (record.get("logs") or {}).items():
            found[f"backend:{record['id']}:{name}"] = str(path)
        return found


def _supervise(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="eks-harness-supervise")
    parser.add_argument("--procs-dir", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--log")
    parser.add_argument("--health")
    parser.add_argument("--health-expect")
    parser.add_argument("--health-grace", type=float, default=90)
    parser.add_argument("--max-restarts", type=int, default=6)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("no command after --")
    registry = ProcessRegistry(Path(args.procs_dir).parent)
    restarts: list[float] = []
    child: subprocess.Popen | None = None
    stopping = False

    def say(message: str) -> None:
        sys.stdout.write(f"[supervisor {iso_now()}] {message}\n")
        sys.stdout.flush()

    def stop_child() -> None:
        if child is None or child.poll() is not None:
            return
        _signal_target(child.pid, not WINDOWS, signal.SIGTERM)
        for _ in range(40):
            if child.poll() is not None:
                return
            time.sleep(0.2)
        if child.poll() is None:
            _signal_target(child.pid, not WINDOWS, signal.SIGKILL if not WINDOWS else signal.SIGTERM)

    def terminate(*_):
        nonlocal stopping
        stopping = True
        stop_child()
        sys.exit(0)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    while not stopping:
        popen_kwargs = {} if WINDOWS else {"process_group": 0}
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, **popen_kwargs)
        started_at = time.time()
        for _ in range(50):
            entry = registry.load(args.key)
            if entry and entry.pid == os.getpid():
                entry.child_pid = child.pid
                entry.child_started = pid_started(child.pid)
                registry.save(entry)
                break
            time.sleep(0.1)
        say(f"child {child.pid} started: {' '.join(command)}")
        failures = 0
        while child.poll() is None:
            time.sleep(2)
            if args.health and time.time() - started_at > args.health_grace:
                healthy = health_ok(args.health, args.health_expect)
                failures = 0 if healthy else failures + 1
                if failures >= 3:
                    say(f"health check {args.health} failed 3 times; restarting child")
                    stop_child()
                    break
        code = child.wait()
        if stopping:
            break
        now = time.time()
        restarts = [stamp for stamp in restarts if now - stamp < 600] + [now]
        say(f"child exited with {code} after {int(now - started_at)}s")
        if len(restarts) > args.max_restarts:
            say(f"{args.max_restarts} restarts in 10 minutes; giving up")
            return 1
        delay = min(30, 2 ** len(restarts))
        say(f"restarting in {delay}s")
        time.sleep(delay)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_supervise":
        sys.exit(_supervise(sys.argv[2:]))
    sys.stderr.write("usage: python -m eks_harness.pools.backends _supervise --procs-dir D --key K -- command...\n")
    sys.exit(64)
