from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from eks_harness.drivers.procs import (
    Owner,
    ProcRecord,
    ProcRegistry,
    pid_alive,
    process_start,
    reap,
    scan_orphans,
    spawn_owned,
    stop_owned_by,
)

ROOT = Path(__file__).resolve().parents[2]
LIFETIME = ROOT / "workers" / "kit" / "src" / "lifetime.mjs"
NODE = shutil.which("node")


def wait_dead(pid: int, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        time.sleep(0.05)
    return not pid_alive(pid)


def test_registry_round_trip_and_reap_of_dead_processes(tmp_path: Path) -> None:
    registry = ProcRegistry(tmp_path / "procs")
    gone = ProcRecord(id="gone", kind="worker.web", pid=999_999, pgid=999_999, started=None, owner_pid=os.getpid(),
                      owner_started=process_start(os.getpid()))
    registry.register(gone)
    assert [record.id for record in registry.records()] == ["gone"]
    (reaped,) = reap(registry)
    assert reaped.reason == "process_gone" and registry.records() == []


def test_reap_stops_processes_whose_owner_died(tmp_path: Path) -> None:
    registry = ProcRegistry(tmp_path / "procs")
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        child, record = spawn_owned([sys.executable, "-c", "import time; time.sleep(60)"], kind="dev.next",
                                    registry=registry, owner=Owner(owner.pid, process_start(owner.pid)))
        assert reap(registry) == [] and child.poll() is None
        owner.kill()
        owner.wait()
        (reaped,) = reap(registry)
        assert reaped.reason == "owner_gone" and reaped.killed
        assert wait_dead(record.pid)
        assert registry.records() == []
    finally:
        owner.kill()


def test_a_reused_owner_pid_is_not_mistaken_for_the_owner(tmp_path: Path) -> None:
    registry = ProcRegistry(tmp_path / "procs")
    child, record = spawn_owned([sys.executable, "-c", "import time; time.sleep(60)"], kind="worker.web",
                                registry=registry, owner=Owner(os.getpid(), "Mon Jan  1 00:00:00 2001"))
    try:
        (reaped,) = reap(registry)
        assert reaped.reason == "owner_gone"
        assert wait_dead(record.pid)
    finally:
        child.kill()


def test_stop_owned_by_stops_only_that_owner(tmp_path: Path) -> None:
    registry = ProcRegistry(tmp_path / "procs")
    mine, _ = spawn_owned([sys.executable, "-c", "import time; time.sleep(60)"], kind="worker.web",
                          registry=registry)
    other = Owner(1, process_start(1))
    theirs, _ = spawn_owned([sys.executable, "-c", "import time; time.sleep(60)"], kind="worker.web",
                            registry=registry, owner=other)
    try:
        stopped = stop_owned_by(registry, Owner.current())
        assert [record.pid for record in stopped] == [mine.pid]
        assert wait_dead(mine.pid) and theirs.poll() is None
    finally:
        theirs.kill()
        mine.kill()


def test_scan_finds_worker_and_harness_dev_orphans_only() -> None:
    rows = [
        "  101     1 Tue Oct 14 10:00:00 2026 node /x/.cache/uv/archive-v0/ab/eks_harness/_workers/web-driver/src/server.mjs",
        "  102     1 Tue Oct 14 10:00:00 2026 node /r/eks-harness/workers/mobile-driver/src/server.mjs",
        "  103   500 Tue Oct 14 10:00:00 2026 node /x/eks_harness/_workers/web-driver/src/server.mjs",
        "  104     1 Tue Oct 14 10:00:00 2026 node vite --config /w/app/.harness/vite.harness.config.mjs",
        "  105     1 Tue Oct 14 10:00:00 2026 node /w/node_modules/.bin/next dev --port 3000",
        "  106     1 Tue Oct 14 10:00:00 2026 /usr/sbin/cron",
    ]
    found = scan_orphans(rows)
    assert [(item.pid, item.kind) for item in found] == [(101, "worker"), (102, "worker"), (104, "harness")]
    with_dev = scan_orphans(rows, include_dev=True)
    assert (105, "dev") in [(item.pid, item.kind) for item in with_dev]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_workers_die_when_their_owner_is_killed_with_sigkill(tmp_path: Path) -> None:
    worker = tmp_path / "worker.mjs"
    worker.write_text(textwrap.dedent(f"""
        import {{ watchOwner }} from {str(LIFETIME)!r}
        watchOwner({{ checkMs: 100 }})
        setInterval(() => {{}}, 1000)
    """), encoding="utf-8")
    pidfile = tmp_path / "worker.pid"
    owner_script = tmp_path / "owner.py"
    owner_script.write_text(textwrap.dedent(f"""
        import time
        from pathlib import Path
        from eks_harness.drivers.procs import ProcRegistry, spawn_owned
        process, _ = spawn_owned([{NODE!r}, {str(worker)!r}], kind="worker.web",
                                 registry=ProcRegistry(Path({str(tmp_path / "procs")!r})))
        Path({str(pidfile)!r}).write_text(str(process.pid))
        time.sleep(120)
    """), encoding="utf-8")
    owner = subprocess.Popen([sys.executable, str(owner_script)])
    try:
        deadline = time.monotonic() + 20
        while not pidfile.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        worker_pid = int(pidfile.read_text())
        time.sleep(0.5)
        assert pid_alive(worker_pid)
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait()
        assert wait_dead(worker_pid), f"worker {worker_pid} outlived its SIGKILLed owner"
    finally:
        owner.kill()
