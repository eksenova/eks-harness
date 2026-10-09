from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eks_harness.daemon.hooks import on_shutdown, on_startup
from eks_harness.daemon.leases import manager as lease_manager
from eks_harness.db.repos import leases as leases_repo
from eks_harness.db.repos import web_sessions as web_sessions_repo
from eks_harness.store import retention as store_retention

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

log = logging.getLogger("eks_harness.housekeeping")

SERVICE = "housekeeping"
ENDED_LEASE_KEEP_DAYS = 90


class Housekeeping:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_sweep = 0.0
        self.last_retention = 0.0
        self.last_prune = 0.0
        self.last_report: dict[str, Any] = {}

    @property
    def sweep_enabled(self) -> bool:
        return not self.ctx.pools.fake

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, name="housekeeping", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)
        self.thread = None

    def loop(self) -> None:
        if self.sweep_enabled:
            try:
                self.sweep()
            except Exception:
                log.exception("initial sweep failed")
        while not self.stop_event.wait(float(self.ctx.config["daemon.tickSeconds"])):
            try:
                self.run_once()
            except Exception:
                log.exception("housekeeping error")
            if self.stop_event.is_set():
                return
            if self.should_exit():
                log.info("nothing running; exiting")
                self.ctx.request_stop()
                return

    def run_once(self, force: bool = False) -> dict[str, Any]:
        stamp = time.time()
        leases = lease_manager(self.ctx)
        report: dict[str, Any] = {"tick": leases.tick()}
        reaped = self.ctx.db.reap()
        if reaped:
            report["closedConnections"] = reaped
        if self.sweep_enabled and (force or stamp - self.last_sweep > float(self.ctx.config["daemon.sweepSeconds"])):
            report["sweep"] = self.sweep()
        if force or stamp - self.last_retention > float(self.ctx.config["retention.checkMinutes"]) * 60:
            report["retention"] = self.retention()
            self.last_retention = stamp
        if force or stamp - self.last_prune > 3600:
            report["pruned"] = self.prune()
            self.last_prune = stamp
        self.last_report = report
        return report

    def sweep(self) -> dict:
        self.last_sweep = time.time()
        return lease_manager(self.ctx).sweep()

    def should_exit(self) -> bool:
        if self.ctx.managed:
            return False
        limit = float(self.ctx.config["daemon.idleExitSeconds"])
        if limit <= 0:
            return False
        leases = lease_manager(self.ctx)
        return leases.nothing_running() and time.time() - leases.last_activity > limit

    def prune(self) -> dict[str, int]:
        stamp = time.time()
        conn = self.ctx.db.conn()
        events = self.ctx.events.prune(float(self.ctx.config["events.retentionDays"]))
        ended = leases_repo.prune_ended(conn, stamp - ENDED_LEASE_KEEP_DAYS * 86400)
        sessions = web_sessions_repo.prune_expired(conn, stamp)
        return {"events": events, "leases": ended, "webSessions": sessions}

    def retention(self) -> dict[str, Any]:
        with self.ctx.pools.host.lock:
            recording = [Path(d["recording"]["dest"]) for d in self.ctx.pools.host.devices.values()
                         if isinstance(d.get("recording"), dict) and d["recording"].get("dest")]
        return store_retention.housekeeping(self.ctx.db, self.ctx.config, self.ctx.paths, events=self.ctx.events,
                                            keep_tmp=recording)


def housekeeping(ctx: "AppContext") -> Housekeeping:
    if not ctx.has_service(SERVICE):
        ctx.register_service(SERVICE, Housekeeping(ctx))
    return ctx.service(SERVICE)


@on_startup(order=30)
def _start(ctx: "AppContext") -> None:
    housekeeping(ctx).start()


@on_shutdown(order=10)
def _stop(ctx: "AppContext") -> None:
    if ctx.has_service(SERVICE):
        ctx.service(SERVICE).stop()
