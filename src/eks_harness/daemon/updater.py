from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from eks_harness import renderq, selfupdate
from eks_harness.daemon.hooks import on_shutdown, on_startup
from eks_harness.db.repos import leases as leases_repo

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

log = logging.getLogger("eks_harness.updater")

SERVICE = "updater"
TICK_SECONDS = 30.0
ACTIVE_JOB_STATES = ("queued", "assigned", "running")


class Updater:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.status: dict[str, Any] | None = None
        self.state = "idle"
        self.message = ""
        self.last_check = 0.0
        self.pending_apply = False
        self.force = False

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, name="updater", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout)
        self.thread = None

    @property
    def automatic(self) -> bool:
        return bool(self.ctx.managed and self.ctx.config["update.auto"] and not self.ctx.pools.fake)

    def snapshot(self) -> dict[str, Any]:
        saved = selfupdate.read_state(self.ctx.paths)
        return {"state": self.state, "message": self.message, "auto": self.automatic,
                "managed": self.ctx.managed, "status": self.status, "lastCheck": self.last_check or None,
                "lastUpdate": saved.get("lastUpdate"), "failed": saved.get("failed"),
                "installed": selfupdate.installed().as_dict(), "busy": self.busy_reasons()}

    def busy_reasons(self) -> list[str]:
        reasons = []
        conn = self.ctx.db.conn()
        live = leases_repo.list_leases(conn, states=leases_repo.LIVE_STATES, limit=50)
        if live:
            reasons.append(f"{len(live)} live lease{'s' if len(live) != 1 else ''}")
        try:
            queued = renderq.status().get("items") or []
        except OSError:
            queued = []
        if queued:
            reasons.append(f"{len(queued)} render{'s' if len(queued) != 1 else ''} running or queued")
        jobs = conn.execute(f"SELECT COUNT(*) FROM node_jobs WHERE state IN ({','.join('?' * len(ACTIVE_JOB_STATES))})",
                            ACTIVE_JOB_STATES).fetchone()[0]
        if jobs:
            reasons.append(f"{jobs} node job{'s' if jobs != 1 else ''} active")
        return reasons

    def check(self) -> dict[str, Any]:
        config = self.ctx.config
        with self.lock:
            self.state, self.message = "checking", ""
        try:
            status = selfupdate.check(config["update.repository"], config["update.ref"]).as_dict()
        except selfupdate.UpdateError as problem:
            with self.lock:
                self.state, self.message = "error", str(problem)
            log.warning("update check failed: %s", problem)
            raise
        with self.lock:
            self.status = status
            self.last_check = time.time()
            self.state = "available" if status["available"] else "idle"
            self.message = status["reason"]
        return status

    def request_apply(self, force: bool = False) -> None:
        self.pending_apply = True
        self.force = force
        self.wake.set()

    def loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except selfupdate.UpdateError:
                pass
            except Exception:
                log.exception("updater error")
            self.wake.wait(TICK_SECONDS)
            self.wake.clear()

    def tick(self) -> None:
        interval = float(self.ctx.config["update.checkMinutes"]) * 60
        due = time.time() - self.last_check >= interval
        if not (self.pending_apply or (self.automatic and due)):
            return
        status = self.check() if due or self.status is None else self.status
        if not status["available"] and not (self.pending_apply and self.force):
            self.pending_apply = False
            return
        installed = status["installed"]
        if not self.pending_apply:
            if installed["source"] in ("directory", "editable") and not self.ctx.config["update.replaceLocal"]:
                with self.lock:
                    self.message = f"{status['reason']}; this install was built from a local checkout (update.replaceLocal is off)"
                return
            failed = selfupdate.read_state(self.ctx.paths).get("failed") or {}
            if failed.get("commit") == status["latest"]:
                return
        busy = self.busy_reasons()
        if busy:
            with self.lock:
                self.state, self.message = "waiting", "waiting until idle: " + ", ".join(busy)
            return
        self.apply(status)

    def apply(self, status: dict[str, Any]) -> None:
        self.pending_apply = self.force = False
        latest = status["latest"]
        extras = status["installed"]["extras"]
        with self.lock:
            self.state, self.message = "installing", f"installing {latest[:12]}"
        log.info("installing eks-harness %s from %s", latest, status["repository"])
        try:
            with selfupdate.update_lock(self.ctx.paths):
                selfupdate.install(status["repository"], latest, extras,
                                   output=lambda line: log.debug("uv: %s", line))
        except selfupdate.UpdateError as problem:
            selfupdate.write_state(self.ctx.paths, failed={"commit": latest, "error": str(problem), "at": time.time()})
            with self.lock:
                self.state, self.message = "error", str(problem)
            log.error("update to %s failed: %s", latest, problem)
            return
        selfupdate.write_state(self.ctx.paths, failed=None, lastUpdate={
            "from": status["installed"]["commit"], "to": latest, "at": time.time(), "by": "daemon"})
        with self.lock:
            self.state, self.message = "restarting", f"installed {latest[:12]}; restarting"
        log.info("installed %s; restarting gracefully", latest)
        if not self.ctx.request_restart():
            with self.lock:
                self.state, self.message = "installed", f"installed {latest[:12]}; restart the daemon to use it"


def updater(ctx: AppContext) -> Updater:
    if not ctx.has_service(SERVICE):
        ctx.register_service(SERVICE, Updater(ctx))
    return ctx.service(SERVICE)


@on_startup(order=90)
def _start(ctx: AppContext) -> None:
    if ctx.pools.fake:
        return
    updater(ctx).start()


@on_shutdown(order=5)
def _stop(ctx: AppContext) -> None:
    if ctx.has_service(SERVICE):
        ctx.service(SERVICE).stop()
