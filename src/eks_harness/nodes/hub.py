from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eks_harness import __version__
from eks_harness.auth.core import sha256_hex
from eks_harness.db.repos import nodes as repo
from eks_harness.ids import new_ulid
from eks_harness.nodes.blobs import BlobStore
from eks_harness.nodes.capabilities import satisfies

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

log = logging.getLogger("eks_harness.nodes")

SERVICE = "nodes"
TOKEN = re.compile(r"ehn_([A-Za-z0-9]{8})_([A-Za-z0-9_-]{20,})")
NODE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PROTOCOL = 1


class NodeError(RuntimeError):
    pass


@dataclass
class Connection:
    node_id: str
    websocket: Any
    loop: asyncio.AbstractEventLoop
    capabilities: dict[str, Any] = field(default_factory=dict)
    slots: list[dict[str, Any]] = field(default_factory=list)
    running: dict[str, str] = field(default_factory=dict)
    version: str = ""
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def send(self, message: dict[str, Any]) -> None:
        text = json.dumps(message, separators=(",", ":"))
        asyncio.run_coroutine_threadsafe(self.websocket.send_text(text), self.loop)

    def busy(self, slot_id: str) -> int:
        return sum(1 for s in self.running.values() if s == slot_id)


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    futures: list[asyncio.Future] = field(default_factory=list)


class NodeHub:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.blobs = BlobStore(ctx.paths.data_dir / "blobs")
        self.lock = threading.RLock()
        self.connections: dict[str, Connection] = {}
        self.waiters: dict[str, _Waiter] = {}

    def create_node(self, node_id: str, *, label: str = "", config: dict[str, Any] | None = None) -> tuple[repo.Node, str]:
        if not NODE_ID.fullmatch(node_id):
            raise NodeError("node ids are lower case letters, digits and dashes")
        prefix, secret = secrets.token_hex(4), secrets.token_urlsafe(32)
        with self.ctx.db.transaction() as conn:
            if repo.get_node(conn, node_id):
                raise NodeError(f"node {node_id} exists")
            node = repo.insert_node(conn, node_id, label, prefix, sha256_hex(secret), config)
        self.ctx.events.publish("node.created", resource=f"node:{node_id}")
        return node, f"ehn_{prefix}_{secret}"

    def rotate_token(self, node_id: str) -> str:
        prefix, secret = secrets.token_hex(4), secrets.token_urlsafe(32)
        with self.ctx.db.transaction() as conn:
            if repo.update_node(conn, node_id, token_prefix=prefix, token_sha256=sha256_hex(secret)) is None:
                raise NodeError(f"no node {node_id}")
        self.disconnect(node_id, "token rotated")
        return f"ehn_{prefix}_{secret}"

    def authenticate(self, raw: str | None) -> repo.Node | None:
        match = TOKEN.fullmatch((raw or "").strip())
        if not match:
            return None
        node = repo.get_node_by_prefix(self.ctx.db.conn(), match.group(1))
        if node is None or node.disabled or not hmac.compare_digest(node.token_sha256, sha256_hex(match.group(2))):
            return None
        return node

    def update_config(self, node_id: str, config: dict[str, Any]) -> repo.Node:
        with self.ctx.db.transaction() as conn:
            node = repo.update_node(conn, node_id, config=config)
        if node is None:
            raise NodeError(f"no node {node_id}")
        connection = self.connections.get(node_id)
        if connection is not None:
            connection.send({"type": "config", "config": config})
        self.schedule()
        return node

    def remove_node(self, node_id: str) -> bool:
        self.disconnect(node_id, "removed")
        with self.ctx.db.transaction() as conn:
            for job in repo.list_jobs(conn, states=repo.LIVE_JOB_STATES, node_id=node_id):
                repo.update_job(conn, job.id, state="queued", node_id=None, slot=None)
            removed = repo.delete_node(conn, node_id)
        self.schedule()
        return removed

    def set_disabled(self, node_id: str, disabled: bool) -> repo.Node:
        with self.ctx.db.transaction() as conn:
            node = repo.update_node(conn, node_id, disabled=disabled)
        if node is None:
            raise NodeError(f"no node {node_id}")
        if disabled:
            self.disconnect(node_id, "disabled")
        return node

    def disconnect(self, node_id: str, reason: str) -> None:
        connection = self.connections.get(node_id)
        if connection is not None:
            asyncio.run_coroutine_threadsafe(connection.websocket.close(code=4000, reason=reason), connection.loop)

    def status(self) -> list[dict[str, Any]]:
        offline_after = float(self.ctx.config["nodes.offlineSeconds"])
        out = []
        for node in repo.list_nodes(self.ctx.db.conn()):
            connection = self.connections.get(node.id)
            item = node.public()
            online = connection is not None and time.time() - connection.last_seen < offline_after
            item["online"] = online
            item["host"] = bool(node.config.get("host"))
            item["state"] = "disabled" if node.disabled else "online" if online else "offline"
            if connection is not None:
                item["version"] = connection.version
                item["capabilities"] = connection.capabilities or node.capabilities
                item["slots"] = [{**slot, "busy": connection.busy(slot["id"])} for slot in self.slots_of(connection)]
                item["running"] = sorted(connection.running)
            else:
                item["slots"] = node.config.get("slots") or []
                item["running"] = []
            out.append(item)
        return out

    def slots_of(self, connection: Connection) -> list[dict[str, Any]]:
        node = repo.get_node(self.ctx.db.conn(), connection.node_id)
        configured = (node.config.get("slots") if node else None) or []
        return configured or connection.slots

    async def serve(self, websocket: Any, node: repo.Node) -> None:
        loop = asyncio.get_running_loop()
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        if hello.get("type") != "hello":
            await websocket.close(code=4002, reason="expected hello")
            return
        previous = self.connections.get(node.id)
        if previous is not None:
            await previous.websocket.close(code=4001, reason="replaced by a new connection")
        connection = Connection(node_id=node.id, websocket=websocket, loop=loop,
                                capabilities=dict(hello.get("capabilities") or {}),
                                slots=list(hello.get("slots") or []), version=str(hello.get("version") or ""))
        with self.lock:
            self.connections[node.id] = connection
        with self.ctx.db.transaction() as conn:
            repo.update_node(conn, node.id, capabilities=connection.capabilities, last_seen_at=time.time())
            for job in repo.list_jobs(conn, states=repo.LIVE_JOB_STATES, node_id=node.id):
                if job.id in set(hello.get("running") or []):
                    connection.running[job.id] = job.slot or ""
                else:
                    self._requeue(conn, job, "node restarted")
        await websocket.send_json({"type": "welcome", "protocol": PROTOCOL, "hub": __version__, "node": node.id,
                                   "config": node.config})
        self.ctx.events.publish("node.connected", resource=f"node:{node.id}",
                                detail={"version": connection.version})
        self.schedule()
        try:
            while True:
                message = await websocket.receive_json()
                connection.last_seen = time.time()
                await asyncio.to_thread(self.handle, connection, message)
        except Exception as error:
            if type(error).__name__ not in ("WebSocketDisconnect", "ConnectionClosed", "ConnectionClosedOK"):
                log.warning("node %s connection ended: %s", node.id, error)
        finally:
            with self.lock:
                if self.connections.get(node.id) is connection:
                    del self.connections[node.id]
            with self.ctx.db.transaction() as conn:
                repo.update_node(conn, node.id, last_seen_at=connection.last_seen)
                for job_id in list(connection.running):
                    job = repo.get_job(conn, job_id)
                    if job is not None and job.live:
                        self._requeue(conn, job, "node disconnected")
            self.ctx.events.publish("node.disconnected", resource=f"node:{node.id}")
            self.schedule()

    def handle(self, connection: Connection, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "ping":
            connection.send({"type": "pong", "t": message.get("t")})
        elif kind == "caps":
            connection.capabilities = dict(message.get("capabilities") or {})
            connection.slots = list(message.get("slots") or connection.slots)
            with self.ctx.db.transaction() as conn:
                repo.update_node(conn, connection.node_id, capabilities=connection.capabilities,
                                 last_seen_at=time.time())
            self.schedule()
        elif kind == "started":
            with self.ctx.db.transaction() as conn:
                repo.update_job(conn, message["job"], state="running", started_at=time.time())
            self._publish(message["job"], "node.job.started")
        elif kind == "progress":
            with self.ctx.db.transaction() as conn:
                repo.update_job(conn, message["job"], progress=float(message.get("progress") or 0),
                                message=str(message.get("message") or "")[:500])
            self.ctx.events.publish("node.job.progress", resource=f"job:{message['job']}", persist=False,
                                    detail={"progress": message.get("progress"), "message": message.get("message"),
                                            "node": connection.node_id})
        elif kind == "done":
            self.finish(connection, message)
        elif kind == "log":
            log.info("node %s: %s", connection.node_id, str(message.get("text"))[:2000])

    def finish(self, connection: Connection, message: dict[str, Any]) -> None:
        job_id = message["job"]
        connection.running.pop(job_id, None)
        ok = bool(message.get("ok"))
        with self.ctx.db.transaction() as conn:
            job = repo.get_job(conn, job_id)
            if job is None or not job.live:
                return
            if ok:
                result = {"data": message.get("result"), "outputs": message.get("outputs") or []}
                repo.update_job(conn, job_id, state="done", result=result, progress=1.0, finished_at=time.time())
            elif message.get("retry") and job.attempts < int(self.ctx.config["nodes.maxAttempts"]):
                self._requeue(conn, job, str(message.get("error") or "failed"))
            else:
                repo.update_job(conn, job_id, state="failed", error=str(message.get("error") or "failed")[:4000],
                                finished_at=time.time())
        self._publish(job_id, "node.job.done" if ok else "node.job.failed")
        self._wake(job_id)
        self.schedule()

    def _requeue(self, conn: Any, job: repo.Job, reason: str) -> None:
        if job.attempts >= int(self.ctx.config["nodes.maxAttempts"]):
            repo.update_job(conn, job.id, state="failed", error=f"{reason} after {job.attempts} attempts",
                            finished_at=time.time())
            self._wake(job.id)
            return
        repo.update_job(conn, job.id, state="queued", node_id=None, slot=None, message=reason)

    def submit(self, kind: str, *, payload: dict[str, Any] | None = None,
               requirements: dict[str, Any] | None = None, inputs: list[dict[str, Any]] | None = None,
               priority: int = 0, owner: str | None = None) -> repo.Job:
        for item in inputs or []:
            if not self.blobs.has(str(item.get("hash", ""))):
                raise NodeError(f"input {item.get('name')} ({item.get('hash')}) is not in the blob store")
        job_id = new_ulid()
        with self.ctx.db.transaction() as conn:
            job = repo.insert_job(conn, job_id, kind, requirements=requirements or {}, payload=payload or {},
                                  inputs=inputs or [], priority=priority, owner=owner)
        with self.lock:
            self.waiters.setdefault(job_id, _Waiter())
        self._publish(job_id, "node.job.queued")
        self.schedule()
        return repo.get_job(self.ctx.db.conn(), job.id)

    def cancel(self, job_id: str) -> repo.Job | None:
        with self.ctx.db.transaction() as conn:
            job = repo.get_job(conn, job_id)
            if job is None or not job.live:
                return job
            repo.update_job(conn, job_id, state="cancelled", finished_at=time.time())
        if job.node_id and job.node_id in self.connections:
            connection = self.connections[job.node_id]
            connection.running.pop(job_id, None)
            connection.send({"type": "cancel", "job": job_id})
        self._publish(job_id, "node.job.cancelled")
        self._wake(job_id)
        self.schedule()
        return repo.get_job(self.ctx.db.conn(), job_id)

    def schedule(self) -> int:
        assigned = 0
        with self.lock:
            queued = repo.queued_jobs(self.ctx.db.conn())
            for job in queued:
                choice = self._pick(job)
                if choice is None:
                    continue
                connection, slot = choice
                with self.ctx.db.transaction() as conn:
                    repo.update_job(conn, job.id, state="assigned", node_id=connection.node_id, slot=slot["id"],
                                    attempts=job.attempts + 1)
                connection.running[job.id] = slot["id"]
                connection.send({"type": "assign", "job": {"id": job.id, "kind": job.kind, "payload": job.payload,
                                                           "inputs": job.inputs, "slot": slot}})
                assigned += 1
        return assigned

    def _pick(self, job: repo.Job) -> tuple[Connection, dict[str, Any]] | None:
        wanted_node = job.requirements.get("node")
        best: tuple[float, Connection, dict[str, Any]] | None = None
        for connection in self.connections.values():
            if wanted_node and connection.node_id != wanted_node:
                continue
            for slot in self.slots_of(connection):
                capacity = int(slot.get("workers") or 1)
                busy = connection.busy(slot["id"])
                if busy >= capacity or not satisfies(connection.capabilities, slot, job.requirements):
                    continue
                score = busy / capacity - float(slot.get("weight") or 0)
                if best is None or score < best[0]:
                    best = (score, connection, slot)
        return (best[1], best[2]) if best else None

    def _publish(self, job_id: str, event: str) -> None:
        job = repo.get_job(self.ctx.db.conn(), job_id)
        if job is not None:
            self.ctx.events.publish(event, resource=f"job:{job_id}", actor=job.owner,
                                    detail={"kind": job.kind, "node": job.node_id, "state": job.state},
                                    persist=event != "node.job.queued")

    def _wake(self, job_id: str) -> None:
        with self.lock:
            waiter = self.waiters.pop(job_id, None)
        if waiter is None:
            return
        waiter.event.set()
        for future in waiter.futures:
            loop = future.get_loop()
            loop.call_soon_threadsafe(lambda f=future: f.done() or f.set_result(True))

    def wait(self, job_id: str, timeout: float | None = None) -> repo.Job:
        job = repo.get_job(self.ctx.db.conn(), job_id)
        if job is None:
            raise NodeError(f"no job {job_id}")
        if not job.live:
            return job
        with self.lock:
            waiter = self.waiters.setdefault(job_id, _Waiter())
        waiter.event.wait(timeout)
        return repo.get_job(self.ctx.db.conn(), job_id)

    async def wait_async(self, job_id: str, timeout: float | None = None) -> repo.Job:
        job = repo.get_job(self.ctx.db.conn(), job_id)
        if job is None:
            raise NodeError(f"no job {job_id}")
        if not job.live:
            return job
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        with self.lock:
            self.waiters.setdefault(job_id, _Waiter()).futures.append(future)
        try:
            await asyncio.wait_for(future, timeout)
        except TimeoutError:
            pass
        return repo.get_job(self.ctx.db.conn(), job_id)


def hub(ctx: AppContext) -> NodeHub:
    if not ctx.has_service(SERVICE):
        ctx.register_service(SERVICE, NodeHub(ctx))
    return ctx.service(SERVICE)
