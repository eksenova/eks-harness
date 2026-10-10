"""Render farms: run a job's work items on local and SSH workers.

Media renderers (``BlenderScene`` and repo-local plugins) hand the farm a
:class:`FarmJob`: a list of integer work items (usually frames), the paths to
mirror, the output directory and a command per worker. Every local path lives
on a remote at ``<root>/abs/<its absolute path>``, so separate trees and
separate projects never overwrite or prune each other. Workers pull items one
at a time over stdin, so fast machines take more of the job; results are
fetched back with rsync while the job runs.

Configuration is a TOML file, first match wins: ``$EKS_HARNESS_FARM_CONFIG``,
``<workspace>/.harness/farm.toml``, ``<eks-harness config dir>/farm.toml``.
``EKS_HARNESS_FARM=0`` disables it. Nothing about a particular setup lives in
code::

    [local]
    enabled = true                  # also render on this machine
    workers_per_slot = 1
    slots = [{}]                    # one entry per device; each may set env

    [[remote]]
    host = "gpu-box"                # ssh destination; ssh config aliases work
    root = "~/.cache/eks-harness-farm" # mirror root on the remote
    workers_per_slot = 2
    slots = [{ env = { CUDA_VISIBLE_DEVICES = "0" } }, { env = { CUDA_VISIBLE_DEVICES = "1" } }]
    env = { SOME_VAR = "1" }        # applies to every slot of this host
    tools = { blender = "~/opt/blender/blender" }

Remote workers need ``ssh`` (key-based, non-interactive) and ``rsync`` on both ends.

Worker protocol: the worker prints ``EHX_READY``; the farm writes one item
per line to its stdin and ``done`` when the queue is empty; the worker prints
``EHX_FRAME <item> ...`` or ``EHX_SKIP <item> ...`` when an item is finished
and may print ``EHX_DEVICE <description>`` once.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import threading
import time
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)
DONE_PREFIXES = ("EHX_FRAME", "EHX_SKIP")
DEFAULT_REMOTE_ROOT = "~/.cache/eks-harness-farm"


@dataclass
class Slot:
    env: dict[str, str] = field(default_factory=dict)
    id: str = ""


@dataclass
class Host:
    name: str
    host: str | None = None
    root: str = ""
    slots: list[Slot] = field(default_factory=lambda: [Slot()])
    workers_per_slot: int = 1
    env: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    home: str = ""
    tag: str = ""
    node: str | None = None
    capabilities: list[str] = field(default_factory=list)

    @property
    def remote(self) -> bool:
        return self.host is not None


@dataclass
class FarmConfig:
    hosts: list[Host]
    source: Path | None = None

    @property
    def remotes(self) -> list[Host]:
        return [h for h in self.hosts if h.remote]


def platform_tag(system: str | None = None, machine: str | None = None) -> str:
    system = system or platform.system()
    machine = machine or platform.machine()
    return f"{ {'Darwin': 'darwin', 'Linux': 'linux', 'Windows': 'windows'}.get(system, system.lower()) }-{machine.lower()}"


def config_path(workspace: Path | None = None) -> Path | None:
    if os.environ.get("EKS_HARNESS_FARM", "1") == "0":
        return None
    candidates = [os.environ.get("EKS_HARNESS_FARM_CONFIG")]
    if workspace is not None:
        candidates.append(str(Path(workspace) / ".harness" / "farm.toml"))
    from eks_harness.paths import resolve_paths

    candidates.append(str(resolve_paths().config_dir / "farm.toml"))
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            return Path(candidate).expanduser()
    return None


def _slots(data: dict) -> list[Slot]:
    slots = [Slot(env={k: str(v) for k, v in (s.get("env") or {}).items()}) for s in data.get("slots") or [{}]]
    return [slot for slot in slots for _ in range(int(data.get("workers_per_slot", 1)))]


def load(workspace: Path | None = None, *, local_workers: int = 1,
         local_slots: list[dict[str, str]] | None = None) -> FarmConfig:
    """The configured farm, or a local-only farm with ``local_workers`` workers when none is configured."""

    path = config_path(workspace)
    if path is None:
        envs = local_slots or [{}]
        slots = [Slot(env=dict(envs[i % len(envs)])) for i in range(max(1, local_workers))]
        return FarmConfig(hosts=[Host(name="local", slots=slots)])
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    hosts: list[Host] = []
    nodes_section = data.get("nodes") or {}
    local = data.get("local", {})
    if local.get("enabled", True):
        hosts.append(Host(name="local", slots=_slots(local), env={k: str(v) for k, v in (local.get("env") or {}).items()},
                          tools={k: str(v) for k, v in (local.get("tools") or {}).items()}))
    for r in data.get("remote", []):
        hosts.append(Host(name=r.get("name", r["host"]), host=r["host"], root=r.get("root", DEFAULT_REMOTE_ROOT),
                          slots=_slots(r), env={k: str(v) for k, v in (r.get("env") or {}).items()},
                          tools={k: str(v) for k, v in (r.get("tools") or {}).items()}))
    if nodes_section.get("enabled"):
        from eks_harness.video.farm.nodes import node_hosts

        hosts.extend(node_hosts(nodes_section, skip_this_machine=bool(local.get("enabled", True))))
    if not hosts:
        raise ValueError(f"{path}: the farm has no local, remote or node workers")
    return FarmConfig(hosts=hosts, source=path)


def _ssh(host: Host, command: str, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", host.host, command], text=True, **kwargs)


@dataclass
class WorkerContext:
    """What a job's command builder knows about the worker it builds for."""

    name: str
    host: Host
    env: dict[str, str]
    index: int
    job: FarmJob
    slot: Slot | None = None

    @property
    def remote(self) -> bool:
        return self.host.remote or self.host.node is not None

    @property
    def tag(self) -> str:
        return self.host.tag

    def path(self, local: Path | str) -> str:
        return self.job.remote_path(Path(local), self.host) if self.remote else str(Path(local))

    def tool(self, name: str, default: str) -> str:
        value = self.host.tools.get(name)
        if not value:
            return default
        return value.replace("~", self.host.home, 1) if self.host.remote and value.startswith("~") else value

    def remap(self, value: Any) -> Any:
        """Map every absolute local path inside ``value`` (str, list, dict) to this worker's filesystem."""

        if not self.remote:
            return value
        if isinstance(value, str) and value.startswith("/") and self.job.maps(Path(value)):
            return self.path(value)
        if isinstance(value, dict):
            return {k: self.remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.remap(v) for v in value]
        return value


@dataclass
class FarmJob:
    name: str
    items: list[int]
    command: Callable[[WorkerContext], list[str]]
    out_dir: Path
    sync: list[Path] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    out_pattern: str = "f_*.png"
    affinity: Callable[[int], str | None] | None = None
    on_line: Callable[[str, str], None] | None = None
    on_progress: Callable[[int, int], None] | None = None
    fetch_every: float = 10.0
    requires: list[str] = field(default_factory=list)
    batch: int = 4

    def maps(self, path: Path) -> bool:
        path = path.resolve()
        return any(path == p.resolve() or p.resolve() in path.parents for p in [*self.sync, self.out_dir])

    def remote_path(self, local: Path, host: Host) -> str:
        return f"{host.root}/abs{local.resolve().as_posix()}"


@dataclass
class _Worker:
    ctx: WorkerContext
    argv: list[str]
    proc: subprocess.Popen | None = None
    current: int | None = None
    done: int = 0
    seconds: float = 0.0
    started: float = 0.0
    device: str = ""
    alive: bool = True
    tail: deque = field(default_factory=lambda: deque(maxlen=40))


@dataclass
class WorkerStats:
    name: str
    done: int
    seconds_per_item: float | None
    device: str


def _prepare_remote(host: Host, job: FarmJob) -> None:
    out = _ssh(host, "printf '%s %s %s' \"$HOME\" \"$(uname -s)\" \"$(uname -m)\"", capture_output=True, check=True)
    home, system, machine = out.stdout.split()
    host.home = home
    host.tag = platform_tag(system, machine)
    if host.root.startswith("~"):
        host.root = host.root.replace("~", home, 1)
    targets = [job.remote_path(p, host) for p in job.sync] + [job.remote_path(job.out_dir, host)]
    _ssh(host, " && ".join(f"mkdir -p {shlex.quote(t if Path(p).is_dir() else str(Path(t).parent))}"
                           for p, t in zip([*job.sync, job.out_dir], targets, strict=True)), check=True)
    excludes = [arg for pattern in job.exclude for arg in ("--exclude", pattern)]
    for path in job.sync:
        src = f"{path}/" if path.is_dir() else str(path)
        dst = f"{host.host}:{job.remote_path(path, host)}{'/' if path.is_dir() else ''}"
        subprocess.run(["rsync", "-a", "--delete", "-e", "ssh -T", *excludes, src, dst], check=True)


def _fetch(host: Host, job: FarmJob, *, check: bool) -> None:
    subprocess.run(["rsync", "-a", "-e", "ssh -T", "--include", job.out_pattern, "--exclude", "*",
                    f"{host.host}:{job.remote_path(job.out_dir, host)}/", f"{job.out_dir}/"],
                   check=check, stdout=subprocess.DEVNULL, stderr=None if check else subprocess.DEVNULL)


def take_item(queues: dict[str | None, deque], tag: str | None, alive: set[str | None]) -> Any:
    """The next item for a worker on platform ``tag``: its own platform's queue first, then unpinned items, then
    items pinned to a platform nobody is serving, and finally the back of the longest queue another live platform
    still has, so an idle worker never sits out while one platform holds all the work."""

    orphaned = [k for k in queues if k not in (tag, None) and k not in alive]
    for key in (tag, None, *sorted(orphaned, key=lambda k: -len(queues[k]))):
        if queues.get(key):
            return queues[key].popleft()
    busy = [k for k in queues if k not in (tag, None) and queues[k]]
    if busy:
        return queues[max(busy, key=lambda k: len(queues[k]))].pop()
    return None


def run(job: FarmJob, farm: FarmConfig) -> list[WorkerStats]:
    """Run ``job`` on every worker of ``farm``; returns per-worker stats. Raises when items stay unfinished."""

    job.out_dir.mkdir(parents=True, exist_ok=True)
    local_tag = platform_tag()
    workers: list[_Worker] = []
    node_session = None
    for host in farm.hosts:
        if host.node is not None:
            if any(need not in host.capabilities for need in job.requires):
                continue
            if node_session is None:
                from eks_harness.video.farm.nodes import NodeFarmSession

                node_session = NodeFarmSession(job)
            host.root = node_session.root
        elif host.remote:
            _LOG.info("farm %s: syncing inputs", host.name)
            _prepare_remote(host, job)
        else:
            host.tag = local_tag
        for i, slot in enumerate(host.slots):
            env = {**host.env, **slot.env}
            ctx = WorkerContext(name=f"{host.name}#{i}" if len(host.slots) > 1 else host.name, host=host, env=env,
                                index=len(workers), job=job, slot=slot)
            argv = job.command(ctx)
            if host.node is not None:
                workers.append(_Worker(ctx=ctx, argv=argv))
                continue
            if host.remote:
                command = " ".join([*(f"{k}={shlex.quote(v)}" for k, v in env.items()), *map(shlex.quote, argv)])
                argv = ["ssh", "-T", "-o", "BatchMode=yes", host.host, command]
            workers.append(_Worker(ctx=ctx, argv=argv))

    queues: dict[str | None, deque] = {}
    for item in job.items:
        queues.setdefault(job.affinity(item) if job.affinity else None, deque()).append(item)
    lock = threading.Lock()
    finished = [0]
    failures: list[str] = []
    stop = threading.Event()

    def take(tag: str) -> int | None:
        return take_item(queues, tag, {w.ctx.tag for w in workers if w.alive})

    def feed(worker: _Worker) -> None:
        assert worker.proc is not None and worker.proc.stdin is not None
        with lock:
            worker.current = take(worker.ctx.tag)
        try:
            worker.proc.stdin.write("done\n" if worker.current is None else f"{worker.current}\n")
            worker.proc.stdin.flush()
            if worker.current is None:
                worker.proc.stdin.close()
            worker.started = time.monotonic()
        except (BrokenPipeError, ValueError):
            pass

    def drive(worker: _Worker) -> None:
        env = None if worker.ctx.remote else {**os.environ, **worker.ctx.env}
        worker.proc = subprocess.Popen(worker.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
        assert worker.proc.stdout is not None
        for line in worker.proc.stdout:
            line = line.rstrip("\n")
            worker.tail.append(line)
            if line.startswith("EHX_DEVICE"):
                worker.device = line.split(" ", 1)[1].strip() if " " in line else ""
            elif line.startswith("EHX_READY"):
                feed(worker)
            elif line.startswith(DONE_PREFIXES):
                if job.on_line:
                    with lock:
                        job.on_line(line, worker.ctx.tag)
                if line.startswith("EHX_FRAME"):
                    worker.done += 1
                    worker.seconds += time.monotonic() - worker.started
                with lock:
                    finished[0] += 1
                    if job.on_progress:
                        job.on_progress(finished[0], len(job.items))
                worker.current = None
                feed(worker)
        code = worker.proc.wait()
        worker.alive = False
        with lock:
            if worker.current is not None:
                queues.setdefault(None, deque()).appendleft(worker.current)
                worker.current = None
            if code != 0:
                failures.append(f"{worker.ctx.name} exited {code}:\n" + "\n".join(worker.tail))

    remotes = {h.name: h for h in farm.hosts if h.remote}

    def fetch_loop(host: Host) -> None:
        while not stop.wait(job.fetch_every):
            _fetch(host, job, check=False)

    def drive_node(worker: _Worker) -> None:
        while not stop.is_set():
            with lock:
                batch = []
                while len(batch) < job.batch:
                    item = take(worker.ctx.tag)
                    if item is None:
                        break
                    batch.append(item)
            if not batch:
                break
            worker.started = time.monotonic()
            try:
                lines = node_session.run_batch(worker.ctx, worker.argv, batch)
            except Exception as error:
                with lock:
                    for item in reversed(batch):
                        queues.setdefault(None, deque()).appendleft(item)
                    failures.append(f"{worker.ctx.name}: {error}")
                break
            elapsed = time.monotonic() - worker.started
            for line in lines:
                worker.tail.append(line)
                if line.startswith("EHX_DEVICE"):
                    worker.device = line.split(" ", 1)[1].strip() if " " in line else ""
                if not line.startswith(DONE_PREFIXES):
                    continue
                if job.on_line:
                    with lock:
                        job.on_line(line, worker.ctx.tag)
                if line.startswith("EHX_FRAME"):
                    worker.done += 1
                with lock:
                    finished[0] += 1
                    if job.on_progress:
                        job.on_progress(finished[0], len(job.items))
            worker.seconds += elapsed
        worker.alive = False

    threads = [threading.Thread(target=drive_node if w.ctx.host.node is not None else drive, args=(w,), daemon=True)
               for w in workers]
    fetchers = [threading.Thread(target=fetch_loop, args=(h,), daemon=True) for h in remotes.values()]
    for t in threads + fetchers:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    for host in remotes.values():
        _fetch(host, job, check=True)
    leftover = sum(len(q) for q in queues.values())
    if leftover or (failures and all(not w.done for w in workers) and job.items):
        raise RuntimeError(f"farm job {job.name!r}: {leftover} items unfinished\n" + "\n\n".join(failures))
    for failure in failures:
        lines = failure.splitlines()
        _LOG.warning("farm job %s: %s\n%s", job.name, lines[0], "\n".join(lines[-12:]))
    return [WorkerStats(w.ctx.name, w.done, (w.seconds / w.done) if w.done else None, w.device) for w in workers]


__all__ = ["FarmConfig", "FarmJob", "Host", "Slot", "WorkerContext", "WorkerStats", "config_path", "load",
           "platform_tag", "run"]
