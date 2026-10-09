from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from eks_harness import __version__
from eks_harness.nodes.blobs import BlobStore
from eks_harness.nodes.capabilities import default_slots, probe
from eks_harness.nodes.jobs import BUILTIN, Handler, JobContext, JobFailed, plugin_handlers
from eks_harness.paths import Paths

log = logging.getLogger("eks_harness.node")

SETTINGS_FILE = "node.json"
PING_SECONDS = 15
CAPS_SECONDS = 300


@dataclass
class NodeSettings:
    hub: str
    token: str
    name: str = ""
    slots: list[dict[str, Any]] = field(default_factory=list)
    cache_dir: str = ""
    blender: str = ""
    env: dict[str, str] = field(default_factory=dict)
    keep_workdirs: bool = False

    @property
    def ws_url(self) -> str:
        base = self.hub.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + "/api/nodes/connect"

    @property
    def http_url(self) -> str:
        base = self.hub.rstrip("/")
        if base.startswith("wss://"):
            return "https://" + base[len("wss://"):]
        if base.startswith("ws://"):
            return "http://" + base[len("ws://"):]
        return base


def settings_file(paths: Paths) -> Path:
    return paths.config_dir / SETTINGS_FILE


def load_settings(paths: Paths) -> NodeSettings:
    file = settings_file(paths)
    data = json.loads(file.read_text(encoding="utf-8"))
    return NodeSettings(hub=data["hub"], token=data["token"], name=data.get("name", ""),
                        slots=list(data.get("slots") or []), cache_dir=data.get("cacheDir", ""),
                        blender=data.get("blender", ""), env=dict(data.get("env") or {}),
                        keep_workdirs=bool(data.get("keepWorkdirs")))


def save_settings(paths: Paths, settings: NodeSettings) -> Path:
    file = settings_file(paths)
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hub": settings.hub, "token": settings.token, "name": settings.name, "slots": settings.slots,
               "cacheDir": settings.cache_dir, "blender": settings.blender, "env": settings.env,
               "keepWorkdirs": settings.keep_workdirs}
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, file)
    return file


class NodeAgent:
    def __init__(self, settings: NodeSettings, paths: Paths, *, handlers: dict[str, Handler] | None = None) -> None:
        self.settings = settings
        self.paths = paths
        root = Path(settings.cache_dir).expanduser() if settings.cache_dir else paths.cache_dir / "node"
        self.blobs = BlobStore(root / "blobs")
        self.work_root = root / "work"
        self.handlers = dict(BUILTIN)
        self.handlers.update(plugin_handlers() if handlers is None else handlers)
        self.capabilities: dict[str, Any] = {}
        self.slots: list[dict[str, Any]] = []
        self.running: dict[str, threading.Event] = {}
        self.outbox: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.websocket: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event = asyncio.Event()
        self.connected = threading.Event()
        self.http = httpx.Client(base_url=settings.http_url, timeout=httpx.Timeout(600, connect=15),
                                 headers={"Authorization": f"Bearer {settings.token}",
                                          "User-Agent": f"eks-harness-node/{__version__}"})

    def refresh_capabilities(self) -> None:
        caps = probe(blender=self.settings.blender or None)
        caps["jobKinds"] = sorted(self.handlers)
        self.capabilities = caps
        self.slots = self.settings.slots or default_slots(caps)

    def hello(self) -> dict[str, Any]:
        with self.lock:
            running = sorted(self.running)
        return {"type": "hello", "version": __version__, "name": self.settings.name,
                "capabilities": self.capabilities, "slots": self.slots, "running": running}

    def send(self, message: dict[str, Any]) -> None:
        loop, websocket = self.loop, self.websocket
        critical = message.get("type") == "done"
        if loop is None or websocket is None:
            if critical:
                with self.lock:
                    self.outbox.append(message)
            return
        future = asyncio.run_coroutine_threadsafe(websocket.send(json.dumps(message)), loop)
        if critical:
            try:
                future.result(timeout=30)
            except Exception:
                with self.lock:
                    self.outbox.append(message)

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.refresh_capabilities)
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                async with connect(self.settings.ws_url, max_size=None,
                                   additional_headers={"Authorization": f"Bearer {self.settings.token}"}) as ws:
                    self.websocket = ws
                    await ws.send(json.dumps(self.hello()))
                    welcome = json.loads(await ws.recv())
                    if welcome.get("type") != "welcome":
                        raise RuntimeError(f"unexpected hub reply {welcome}")
                    log.info("connected to %s as %s", self.settings.hub, welcome.get("node"))
                    self.connected.set()
                    delay = 1.0
                    with self.lock:
                        pending, self.outbox = self.outbox, []
                    for message in pending:
                        await ws.send(json.dumps(message))
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = getattr(getattr(error, "rcvd", None), "code", None)
                if code in (4401, 4001) or "4401" in str(error):
                    log.error("hub refused the node token")
                log.warning("hub connection lost: %s", error)
            finally:
                self.websocket = None
                self.connected.clear()
            if self.stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 30.0)

    async def _session(self, ws: Any) -> None:
        last_ping = last_caps = time.time()
        while not self.stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=PING_SECONDS)
            except TimeoutError:
                raw = None
            now = time.time()
            if now - last_ping >= PING_SECONDS:
                await ws.send(json.dumps({"type": "ping", "t": now}))
                last_ping = now
            if now - last_caps >= CAPS_SECONDS:
                await asyncio.to_thread(self.refresh_capabilities)
                await ws.send(json.dumps({"type": "caps", "capabilities": self.capabilities, "slots": self.slots}))
                last_caps = now
            if raw is None:
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "assign":
                self.start_job(message["job"])
            elif kind == "cancel":
                with self.lock:
                    event = self.running.get(message.get("job"))
                if event is not None:
                    event.set()
            elif kind == "config":
                slots = (message.get("config") or {}).get("slots")
                if slots:
                    self.slots = list(slots)

    def stop(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
            websocket = self.websocket
            if websocket is not None:
                asyncio.run_coroutine_threadsafe(websocket.close(), self.loop)
        with self.lock:
            for event in self.running.values():
                event.set()

    def start_job(self, job: dict[str, Any]) -> None:
        cancelled = threading.Event()
        with self.lock:
            if job["id"] in self.running:
                return
            self.running[job["id"]] = cancelled
        thread = threading.Thread(target=self._execute, args=(job, cancelled), name=f"job-{job['id']}", daemon=True)
        thread.start()

    def fetch(self, digest: str) -> Path:
        if not self.blobs.has(digest):
            with self.http.stream("GET", f"/api/blobs/{digest}") as response:
                if response.status_code != 200:
                    raise JobFailed(f"input {digest} could not be fetched ({response.status_code})", retry=True)
                self.blobs.put_stream(response.iter_bytes(1 << 20), digest)
        return self.blobs.path(digest)

    def upload(self, path: Path, name: str) -> dict[str, Any]:
        digest, size = self.blobs.put_file(path)
        head = self.http.head(f"/api/blobs/{digest}")
        if head.status_code != 200:
            response = self.http.put(f"/api/blobs/{digest}", content=self.blobs.iter_chunks(digest))
            if response.status_code != 200:
                raise JobFailed(f"upload of {name} failed ({response.status_code}: {response.text[:200]})", retry=True)
        return {"name": name, "hash": digest, "size": size}

    def _execute(self, job: dict[str, Any], cancelled: threading.Event) -> None:
        job_id = job["id"]
        slot = dict(job.get("slot") or {})
        workdir = self.work_root / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        message: dict[str, Any] = {"type": "done", "job": job_id, "ok": False}
        try:
            handler = self.handlers.get(job["kind"])
            if handler is None:
                raise JobFailed(f"this node cannot run '{job['kind']}' jobs")
            self.send({"type": "started", "job": job_id})
            inputs: dict[str, Path] = {}
            for item in job.get("inputs") or []:
                name = str(item.get("path") or item.get("name") or item["hash"])
                target = (workdir / name).resolve()
                if not target.is_relative_to(workdir.resolve()):
                    raise JobFailed(f"input path {name} leaves the work directory")
                self.fetch(item["hash"])
                self.blobs.materialize(item["hash"], target)
                inputs[str(item.get("name") or name)] = target
            env = {**os.environ, **self.settings.env, **{k: str(v) for k, v in (slot.get("env") or {}).items()}}
            env["EKS_HARNESS_JOB"] = job_id
            env["EKS_HARNESS_SLOT"] = str(slot.get("id") or "")
            last = [0.0]

            def progress(fraction: float, text: str = "") -> None:
                now = time.time()
                if now - last[0] >= 0.5 or fraction >= 1:
                    last[0] = now
                    self.send({"type": "progress", "job": job_id, "progress": fraction, "message": text})

            def log_line(text: str) -> None:
                self.send({"type": "log", "job": job_id, "text": text})

            ctx = JobContext(id=job_id, kind=job["kind"], payload=dict(job.get("payload") or {}), slot=slot,
                             workdir=workdir, inputs=inputs, env=env, cancelled=cancelled, progress=progress,
                             log=log_line, upload=self.upload)
            result = handler(ctx)
            message.update({"ok": True, "result": result, "outputs": ctx.outputs})
        except JobFailed as error:
            message.update({"error": str(error), "retry": error.retry})
        except Exception as error:
            log.exception("job %s failed", job_id)
            message.update({"error": f"{type(error).__name__}: {error}", "retry": False})
        finally:
            with self.lock:
                self.running.pop(job_id, None)
            if not self.settings.keep_workdirs:
                shutil.rmtree(workdir, ignore_errors=True)
        if not cancelled.is_set() or message.get("ok"):
            self.send(message)


def serve(paths: Paths, settings: NodeSettings | None = None) -> None:
    agent = NodeAgent(settings or load_settings(paths), paths)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        agent.stop()
