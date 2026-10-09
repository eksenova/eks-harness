from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

_LOG = logging.getLogger(__name__)
_LOCAL = threading.local()
POLL_SECONDS = 0.5
HOLDER_ENV = "EKS_HARNESS_RENDER_SLOT"


@dataclass
class Ticket:
    id: str
    kind: str
    label: str
    session: str | None
    pid: int
    host: str
    queued_at: float
    started_at: float | None = None
    leases: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def queue_dir() -> Path:
    from eks_harness.paths import resolve_paths

    folder = resolve_paths().state_dir / "render-queue"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def concurrency() -> int:
    try:
        from eks_harness.config import Config
        from eks_harness.paths import resolve_paths

        return max(1, int(Config(resolve_paths())["render.concurrency"] or 1))
    except Exception:
        return 1


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(path: Path) -> Ticket | None:
    try:
        return Ticket(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def _write(folder: Path, ticket: Ticket) -> None:
    target = folder / f"{ticket.id}.json"
    temp = folder / f".{ticket.id}.tmp"
    temp.write_text(json.dumps(ticket.as_dict()), encoding="utf-8")
    os.replace(temp, target)


def tickets(folder: Path | None = None, *, prune: bool = True) -> list[Ticket]:
    folder = folder or queue_dir()
    host = socket.gethostname()
    found = []
    for path in folder.glob("*.json"):
        ticket = _read(path)
        if ticket is None:
            continue
        if ticket.host == host and not _alive(ticket.pid):
            if prune:
                path.unlink(missing_ok=True)
            continue
        found.append(ticket)
    return sorted(found, key=lambda t: (t.started_at is None, t.started_at or t.queued_at, t.queued_at, t.id))


def status(folder: Path | None = None) -> dict:
    items = tickets(folder)
    running = [t for t in items if t.started_at is not None]
    waiting = [t for t in items if t.started_at is None]
    now = time.time()
    rows = []
    for t in running:
        rows.append({**t.as_dict(), "state": "running", "seconds": round(now - (t.started_at or now), 1)})
    for position, t in enumerate(waiting, start=1):
        rows.append({**t.as_dict(), "state": "waiting", "position": position, "seconds": round(now - t.queued_at, 1)})
    return {"concurrency": concurrency(), "running": len(running), "waiting": len(waiting), "items": rows}


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    lock = folder / "queue.lock"
    with open(lock, "a+") as handle:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        except ImportError:
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
            except ImportError:
                pass


def _held_by_parent(items: list[Ticket]) -> bool:
    holder = os.environ.get(HOLDER_ENV)
    return bool(holder) and any(t.id == holder and t.started_at is not None and t.pid != os.getpid() for t in items)


def holding(ticket_id: str | None, items: list[Ticket] | None = None) -> bool:
    if not ticket_id:
        return False
    return any(t.id == ticket_id and t.started_at is not None for t in (items if items is not None else tickets()))


def current_holder() -> str | None:
    return os.environ.get(HOLDER_ENV) or None


def attach_lease(sid: str | None, ticket_id: str | None = None) -> bool:
    ticket_id = ticket_id or current_holder()
    if not sid or not ticket_id:
        return False
    folder = queue_dir()
    with _locked(folder):
        path = folder / f"{ticket_id}.json"
        ticket = _read(path)
        if ticket is None:
            return False
        if sid not in ticket.leases:
            ticket.leases.append(sid)
            _write(folder, ticket)
    return True


def lease_holder(sid: str | None, items: list[Ticket] | None = None) -> str | None:
    if not sid:
        return None
    for ticket in items if items is not None else tickets():
        if ticket.started_at is not None and sid in ticket.leases:
            return ticket.id
    return None


@contextlib.contextmanager
def render_slot(kind: str, label: str, *, session: str | None = None, inherit: str | None = None,
                lease: str | None = None,
                on_wait: Callable[[int, Ticket], None] | None = None) -> Iterator[Ticket | None]:
    depth = getattr(_LOCAL, "depth", 0)
    if depth:
        _LOCAL.depth = depth + 1
        try:
            yield None
        finally:
            _LOCAL.depth -= 1
        return
    folder = queue_dir()
    current = tickets(folder)
    if _held_by_parent(current) or holding(inherit, current) or lease_holder(lease, current):
        yield None
        return
    ticket = Ticket(id=f"{time.time_ns()}-{os.getpid()}-{secrets.token_hex(3)}", kind=kind, label=label[:200],
                    session=session or os.environ.get("EKS_HARNESS_SESSION") or os.environ.get("EKS_HARNESS_SID"),
                    pid=os.getpid(), host=socket.gethostname(), queued_at=time.time())
    _write(folder, ticket)
    reported = None
    try:
        while True:
            with _locked(folder):
                items = tickets(folder)
                running = [t for t in items if t.started_at is not None]
                waiting = [t for t in items if t.started_at is None]
                position = next((i for i, t in enumerate(waiting) if t.id == ticket.id), 0)
                if len(running) + position < concurrency():
                    ticket.started_at = time.time()
                    _write(folder, ticket)
                    break
            if position != reported:
                reported = position
                _LOG.info("render queue: %s %s waits at position %d", kind, label, position + 1)
                if on_wait is not None:
                    on_wait(position + 1, ticket)
            time.sleep(POLL_SECONDS)
        _LOCAL.depth = 1
        previous = os.environ.get(HOLDER_ENV)
        os.environ[HOLDER_ENV] = ticket.id
        try:
            yield ticket
        finally:
            _LOCAL.depth = 0
            if previous is None:
                os.environ.pop(HOLDER_ENV, None)
            else:
                os.environ[HOLDER_ENV] = previous
    finally:
        (folder / f"{ticket.id}.json").unlink(missing_ok=True)
