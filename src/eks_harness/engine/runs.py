from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from eks_harness import project_settings
from eks_harness.api.routes_leases import authorize_target
from eks_harness.daemon.hooks import on_shutdown, on_startup
from eks_harness.data.context import SeedError
from eks_harness.data.engine import SeedEngine
from eks_harness.db.repos import grants, projects, sessions
from eks_harness.drivers.cassettes import FileCassetteHook
from eks_harness.drivers.devices import PoolDeviceCapture
from eks_harness.drivers.port import WorkerDriverProvider, worker_driver_problem
from eks_harness.drivers.registry import driver_extensions
from eks_harness.engine.admission import Admission, AdmittedRun
from eks_harness.engine.auth import PersonaAuthRegistry
from eks_harness.engine.build import OneShotRequest, PlanBuilder
from eks_harness.engine.cache import CacheService, ContentResolver
from eks_harness.engine.debugger import Debugger, parse_breakpoint
from eks_harness.engine.drivers import DriverPorts, DriverProvider, DriverRegistry
from eks_harness.engine.errors import (
    AdmissionError,
    ControlError,
    EngineError,
    NodeFailure,
    ResourceError,
    RunNotFound,
)
from eks_harness.engine.events import RUN_CREATED, RunEvents
from eks_harness.engine.executors import (
    DaemonArtifacts,
    ExecutorChoice,
    ExecutorRegistry,
)
from eks_harness.engine.journal import Journal, RunJournal, RunRow
from eks_harness.engine.model import (
    TERMINAL_STATUSES,
    ControlRequest,
    ControlResponse,
    DebuggerState,
    DoRequest,
    DoResult,
    IssueOut,
    JournalPage,
    NodeEventRecord,
    PauseInfo,
    RunComparison,
    RunInfo,
    RunPage,
    RunQuery,
    RunReport,
    RunRequest,
    RunStatus,
    RunSummary,
)
from eks_harness.engine.registries import HubRegistries
from eks_harness.engine.report import build_report, compare_runs, summary
from eks_harness.engine.report import journal_page as build_journal_page
from eks_harness.engine.resources import (
    KEEPALIVE_SECONDS,
    CassetteHooks,
    HeldResources,
    ResourcePlanner,
    ResourceSink,
    SeedHooks,
)
from eks_harness.engine.sandbox import SandboxLimits, SandboxRunner
from eks_harness.engine.scheduler import Scheduler, SchedulerConfig, SchedulerDeps
from eks_harness.ids import new_ulid
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import OpSpec, Registry
from eks_harness.ops import register_builtin_executors
from eks_harness.ops.fakes import FAKE_PLATFORMS, FakeDriverProvider, FakeSeedHook

if TYPE_CHECKING:
    from eks_harness.auth.core import Principal
    from eks_harness.daemon.context import AppContext

SERVICE = "engine"
MAINTENANCE_SECONDS = 300.0
SEED_SNAPSHOT_EVENT = "seed.snapshot"
SEED_DIVERGED_EVENT = "seed.diverged"
RECOVERY_REASON = "interrupted: the hub restarted while the run was active"

log = logging.getLogger("eks_harness.engine.runs")

_SHUTDOWN_REASON = "the hub shut down while the run was active"
_QUEUED_REASON = "cancelled before it started"
_DEBUGGER_ABORT = "aborted from the debugger"
_WAIT_SLICE = 0.25
_CANCEL_WAIT = 30.0
_CONTROL_TIMEOUT = 30.0
_SHUTDOWN_JOIN = 30.0
_DAY_SECONDS = 86400.0
_GIB = 1024 ** 3
_SCRATCH = "engine"
_RUNS_SCRATCH = "runs"
_TERMINAL: frozenset[RunStatus] = frozenset({"passed", "failed", "aborted", "error"})
_SETTLED: frozenset[RunStatus] = frozenset({"passed", "failed", "aborted", "error", "paused"})


@dataclass
class ActiveRun:
    admitted: AdmittedRun
    thread: threading.Thread | None = None
    scheduler: Scheduler | None = None
    done: threading.Event | None = None


@dataclass
class _RunState:
    writer: RunJournal
    cancel: threading.Event = field(default_factory=threading.Event)
    abort_reason: str | None = None
    shutdown: bool = False


@dataclass
class _Counter:
    value: int = 0


class _Publisher:
    def __init__(self, events: RunEvents, changed: threading.Condition, counter: _Counter) -> None:
        self.events = events
        self.changed = changed
        self.counter = counter

    def journaled(self, info: RunInfo, record: NodeEventRecord) -> None:
        self.events.journaled(info, record)
        if record.type.startswith("run."):
            with self.changed:
                self.counter.value += 1
                self.changed.notify_all()

    def activity(self, info: RunInfo, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.activity(info, type, detail)

    def progress(self, info: RunInfo, detail: Mapping[str, JsonValue]) -> None:
        self.events.progress(info, detail)


class RunManager:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._lock = threading.RLock()
        self._active: dict[str, ActiveRun] = {}
        self.registries = HubRegistries(ctx.plugins)
        self.executors = ExecutorRegistry()
        self.drivers = DriverRegistry()
        self.auth = PersonaAuthRegistry()
        self.seeds = SeedHooks()
        self.cassettes = CassetteHooks()
        self.cassettes.register(FileCassetteHook(ctx.paths.state_dir / "cassettes"))
        self.seed_engine = SeedEngine(ctx, self.auth)
        self.seeds.register("*", self.seed_engine)
        self.journal = Journal(ctx.db)
        self.events = RunEvents(ctx.events)
        self.artifacts = DaemonArtifacts(ctx.db, ctx.config, ctx.events)
        self.builder = PlanBuilder(self.registries)
        self.planner = ResourcePlanner(ctx, seeds=self.seeds, cassettes=self.cassettes)
        self.admission = Admission(builder=self.builder, executors=self.executors, drivers=self.drivers,
                                   planner=self.planner, seeds=self.seeds, cassettes=self.cassettes,
                                   devices=ctx.pools.devices.available,
                                   builtin_drivers=lambda platform: worker_driver_problem(platform))
        register_builtin_executors(self.executors, self.auth,
                                   api_timeout=float(ctx.config["engine.apiTimeoutSeconds"]), seeds=self.seed_engine)
        cache = CacheService(ctx.paths.cache_dir, ctx.db,
                             max_bytes=int(ctx.config["engine.cacheMaxGb"]) * _GIB,
                             max_age_days=float(ctx.config["engine.cacheMaxAgeDays"]))
        timeout = float(ctx.config["engine.sandboxTimeoutSeconds"])
        sandbox = SandboxRunner(limits=SandboxLimits(timeout_seconds=timeout, cpu_seconds=int(timeout) + 1,
                                                     memory_mb=int(ctx.config["engine.sandboxMemoryMb"])),
                                scratch=ctx.paths.tmp_dir / _SCRATCH)
        self.cache: CacheService | None = cache
        self.sandbox: SandboxRunner | None = sandbox
        self._maintenance: threading.Thread | None = None
        self._stopping = threading.Event()
        self._cache_service = cache
        self._sandbox_runner = sandbox
        self._states: dict[str, _RunState] = {}
        self._queue: list[str] = []
        self._changed = threading.Condition()
        self._version = _Counter()
        self._publisher = _Publisher(self.events, self._changed, self._version)
        self._slugs: dict[int, str] = {}

    def start(self, request: RunRequest, principal: Principal | None) -> RunSummary:
        if self._stopping.is_set():
            raise EngineError("the hub is shutting down; start the run again once it is back", code="hub_stopping")
        _check_breakpoints(request.options.breakpoints)
        run_id = new_ulid()
        admitted = self.admission.admit(request, run_id=run_id, created_at=time.time(),
                                        actor=principal.username if principal is not None else None,
                                        user_id=principal.user_id if principal is not None else None)
        if principal is not None:
            authorize_target(self.ctx, principal, admitted.info.project, admitted.info.session)
        with self.ctx.db.transaction() as conn:
            projects.ensure(conn, admitted.info.project)
            session, _created = sessions.ensure_in_project(conn, admitted.info.project, admitted.info.session)
        self._slugs[session.id] = session.slug
        info = admitted.info.model_copy(update={"session_id": session.id})
        admitted = replace(admitted, info=info)
        self.journal.create_run(info, admitted.plan,
                                request=request.model_dump(mode="json", by_alias=True, exclude={"plan"}),
                                environment=admitted.environment, simulated=admitted.simulated,
                                replay_of=request.replay_of, repeat_of=request.repeat_of)
        writer = RunJournal(self.journal, self._publisher, info)
        writer.status("queued")
        for issue in admitted.issues:
            writer.event("run.warning", detail=issue.model_dump(mode="json", by_alias=True))
        self.events.activity(info, RUN_CREATED, {"status": "queued", "reason": None})
        with self._lock:
            self._active[run_id] = ActiveRun(admitted=admitted, done=threading.Event())
            self._states[run_id] = _RunState(writer=writer)
            self._queue.append(run_id)
        self._dispatch()
        return self.status(run_id)

    def status(self, run_id: str) -> RunSummary:
        return self._summary(self._row(run_id))

    def report(self, run_id: str) -> RunReport:
        row = self._row(run_id)
        report = build_report(self.journal, run_id, self.ctx.links, self.artifacts, pause=self._pause(run_id),
                              session_slug=self._slug(row))
        return report.model_copy(update={"run": self._summary(self._row(run_id))})

    def journal_page(self, run_id: str, *, events: bool = False, after: int | None = None,
                     limit: int = 500) -> JournalPage:
        row = self._row(run_id)
        page = build_journal_page(self.journal, run_id, self.ctx.links, events=events, after=after, limit=limit,
                                  pause=self._pause(run_id), session_slug=self._slug(row))
        return page.model_copy(update={"run": self._summary(row)})

    def list(self, query: RunQuery, principal: Principal | None) -> RunPage:
        if principal is None or principal.is_admin:
            rows, cursor = self.journal.runs(query)
            return RunPage(items=[self._summary(row) for row in rows], next_cursor=cursor)
        scope = grants.scope_for_user(self.ctx.db.conn(), principal.user_id, principal.role)
        rows, cursor = self.journal.runs(query, project_ids=sorted(scope.projects),
                                         session_ids=sorted(scope.sessions))
        visible = [row for row in rows if scope.can(row.info.project or None, row.info.session_id, "viewer")]
        return RunPage(items=[self._summary(row) for row in visible], next_cursor=cursor)

    def wait(self, run_id: str, timeout: float, *, until: frozenset[RunStatus] | None = None) -> RunSummary:
        wanted = until if until is not None else _SETTLED
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            with self._changed:
                seen = self._version.value
            row = self._row(run_id)
            remaining = deadline - time.monotonic()
            if row.status in wanted or remaining <= 0 or not self._is_active(run_id):
                return self._summary(row)
            with self._changed:
                if self._version.value == seen:
                    self._changed.wait(min(remaining, _WAIT_SLICE))

    def control(self, run_id: str, request: ControlRequest, actor: str | None = None, *,
                timeout: float = _CONTROL_TIMEOUT) -> ControlResponse:
        row = self._row(run_id)
        if row.status in TERMINAL_STATUSES:
            raise ControlError(f"run {run_id} already ended ({row.status}); replay it to run the plan again",
                               code="run_finished", detail={"run": run_id, "status": row.status})
        with self._lock:
            active = self._active.get(run_id)
            state = self._states.get(run_id)
            scheduler = active.scheduler if active is not None else None
        writer = state.writer if state is not None else RunJournal(self.journal, self._publisher, row.info)
        writer.event("control", detail={"action": request.action, "actor": actor})
        if scheduler is not None:
            return scheduler.control(request, timeout=timeout)
        debugger = _debugger_state(row)
        if request.action == "abort":
            ended = self.cancel(run_id, _DEBUGGER_ABORT, actor)
            return ControlResponse(ok=True, status=ended.status, debugger="off",
                                   message=f"the run ended {ended.status} before it executed any node")
        if request.action == "status":
            return ControlResponse(ok=True, status=row.status, debugger=debugger, message=f"the run is {row.status}")
        return ControlResponse(ok=False, status=row.status, debugger=debugger,
                               message=f"the run is {row.status}; {request.action} needs a run that executes nodes")

    def cancel(self, run_id: str, reason: str, actor: str | None = None) -> RunSummary:
        row = self._row(run_id)
        if row.status in TERMINAL_STATUSES:
            return self._summary(row)
        with self._lock:
            active = self._active.get(run_id)
            state = self._states.get(run_id)
            queued = run_id in self._queue
            scheduler: Scheduler | None = None
            if queued:
                self._queue.remove(run_id)
                self._active.pop(run_id, None)
                self._states.pop(run_id, None)
            elif state is not None:
                if state.abort_reason is None:
                    state.abort_reason = reason
                state.cancel.set()
                scheduler = active.scheduler if active is not None else None
        if queued or active is None or state is None:
            writer = state.writer if state is not None else RunJournal(self.journal, self._publisher, row.info)
            writer.status("aborted", reason=_QUEUED_REASON if queued else reason)
            if active is not None and active.done is not None:
                active.done.set()
            self._notify()
            self._dispatch()
            return self.status(run_id)
        if scheduler is not None:
            scheduler.abort(state.abort_reason or reason)
        if active.done is not None:
            active.done.wait(_CANCEL_WAIT)
        return self.status(run_id)

    def replay(self, run_id: str, principal: Principal | None) -> RunSummary:
        row = self._row(run_id)
        stored = RunRequest.model_validate(row.request)
        request = stored.model_copy(update={
            "plan": row.plan, "spec": None, "source": None, "entry": None, "tree": row.info.tree,
            "project": row.info.project or None, "session": row.info.session or None, "replay_of": run_id,
            "repeat_of": None})
        return self.start(request, principal)

    def note(self, run_id: str, type: str, detail: Mapping[str, JsonValue]) -> None:
        row = self._row(run_id)
        with self._lock:
            state = self._states.get(run_id)
        writer = state.writer if state is not None else RunJournal(self.journal, self._publisher, row.info)
        writer.event(type, detail=detail)

    def compare(self, a: str, b: str) -> RunComparison:
        self._row(a)
        self._row(b)
        return compare_runs(a, self.journal.nodes(a), b, self.journal.nodes(b))

    def do(self, op: str, request: DoRequest, principal: Principal | None) -> DoResult:
        tree = Path(request.tree).expanduser() if request.tree else None
        built = self.builder.oneshot(OneShotRequest(op=op, args=dict(request.args), platform=request.platform,
                                                    app=request.app, persona=request.persona, seed=request.seed,
                                                    tree=tree))
        started = self.start(RunRequest(plan=built.plan, tree=str(built.tree) if built.tree is not None else None,
                                        project=request.project, session=request.session,
                                        instance=request.instance, backend=request.backend, fakes=request.fakes),
                             principal)
        self.wait(started.id, request.wait, until=_TERMINAL)
        report = self.report(started.id)
        values = report.values
        value: JsonValue = values.get("value") if isinstance(values, dict) else None
        failure = report.failures[0] if report.failures and report.run.status != "passed" else None
        return DoResult(run=report.run, value=value, artifacts=report.artifacts, failure=failure)

    def recover(self) -> list[str]:
        recovered: list[str] = []
        for run_id in self.journal.unfinished():
            if self._is_active(run_id):
                continue
            row = self.journal.run(run_id)
            if row is None or row.status in TERMINAL_STATUSES:
                continue
            RunJournal(self.journal, self._publisher, row.info).status("error", reason=RECOVERY_REASON)
            recovered.append(run_id)
        return recovered

    def maintain(self) -> dict[str, int]:
        days = float(self.ctx.config["engine.runRetentionDays"])
        pruned = self.journal.prune(time.time() - days * _DAY_SECONDS)
        evicted = self._cache_service.evict()
        return {"runs": pruned, "entries": evicted.removed, "bytes": evicted.freed, "blobs": evicted.blobs}

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            queued = list(self._queue)
            self._queue.clear()
            dropped = [(self._active.pop(run_id), self._states.pop(run_id, None)) for run_id in queued
                       if run_id in self._active]
            running = list(self._active.items())
            for run_id, _active in running:
                state = self._states.get(run_id)
                if state is None:
                    continue
                state.shutdown = True
                if state.abort_reason is None:
                    state.abort_reason = _SHUTDOWN_REASON
                state.cancel.set()
        for active, state in dropped:
            writer = state.writer if state is not None else RunJournal(self.journal, self._publisher,
                                                                       active.admitted.info)
            writer.status("error", reason=_SHUTDOWN_REASON)
            if active.done is not None:
                active.done.set()
        for _run_id, active in running:
            if active.scheduler is not None:
                active.scheduler.abort(_SHUTDOWN_REASON)
        deadline = time.monotonic() + _SHUTDOWN_JOIN
        for run_id, active in running:
            thread = active.thread
            if thread is None:
                continue
            thread.join(max(deadline - time.monotonic(), 0.0))
            if thread.is_alive():
                log.error("run %s did not stop within %s s of the hub shutdown", run_id, _SHUTDOWN_JOIN)
        maintenance = self._maintenance
        if maintenance is not None:
            maintenance.join(max(deadline - time.monotonic(), 1.0))
            self._maintenance = None
        self._notify()

    def _start_maintenance(self) -> None:
        with self._lock:
            if self._maintenance is not None or self._stopping.is_set():
                return
            thread = threading.Thread(target=self._maintain_loop, name="eks-engine-maintenance", daemon=True)
            self._maintenance = thread
        thread.start()

    def _maintain_loop(self) -> None:
        while not self._stopping.wait(MAINTENANCE_SECONDS):
            try:
                self.maintain()
            except Exception:
                log.exception("engine maintenance failed")

    def _row(self, run_id: str) -> RunRow:
        row = self.journal.run(run_id)
        if row is None:
            raise RunNotFound(f"run {run_id} not found", detail={"run": run_id})
        return row

    def _is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def _notify(self) -> None:
        with self._changed:
            self._version.value += 1
            self._changed.notify_all()

    def _slug(self, row: RunRow) -> str | None:
        session_id = row.info.session_id
        if session_id is None:
            return None
        cached = self._slugs.get(session_id)
        if cached is not None:
            return cached
        found = sessions.get(self.ctx.db.conn(), session_id)
        if found is None:
            return None
        self._slugs[session_id] = found.slug
        return found.slug

    def _scheduler(self, run_id: str) -> Scheduler | None:
        with self._lock:
            active = self._active.get(run_id)
            return active.scheduler if active is not None else None

    def _pause(self, run_id: str) -> PauseInfo | None:
        scheduler = self._scheduler(run_id)
        return scheduler.deps.debugger.pause_info if scheduler is not None else None

    def _summary(self, row: RunRow) -> RunSummary:
        scheduler = self._scheduler(row.info.id)
        base = summary(row, self.ctx.links, session_slug=self._slug(row))
        if scheduler is None or row.status in TERMINAL_STATUSES:
            return base
        live = scheduler.status()
        pause = scheduler.deps.debugger.pause_info if row.status == "paused" else None
        return base.model_copy(update={"counts": dict(live.counts), "cache": live.cache, "pause": pause})

    def _live_status(self, run_id: str) -> RunStatus:
        scheduler = self._scheduler(run_id)
        if scheduler is not None:
            return scheduler.status().status
        row = self.journal.run(run_id)
        return row.status if row is not None else "error"

    def _dispatch(self) -> None:
        if self._stopping.is_set():
            return
        limit = max(int(self.ctx.config["engine.maxRuns"]), 1)
        started: list[threading.Thread] = []
        with self._lock:
            busy = sum(1 for active in self._active.values() if active.thread is not None)
            while busy < limit and self._queue:
                run_id = self._queue.pop(0)
                active = self._active.get(run_id)
                if active is None:
                    continue
                thread = threading.Thread(target=self._execute, args=(run_id,), name=f"eks-run-{run_id}",
                                          daemon=True)
                active.thread = thread
                started.append(thread)
                busy += 1
        for thread in started:
            thread.start()

    def _execute(self, run_id: str) -> None:
        with self._lock:
            active = self._active[run_id]
            state = self._states[run_id]
        held: list[HeldResources] = []
        ports: list[DriverPorts] = []
        stop = threading.Event()
        scratch = self.ctx.paths.tmp_dir / _SCRATCH / _RUNS_SCRATCH / run_id
        status: RunStatus = "error"
        reason: str | None = None
        try:
            status, reason = self._run(active, state, held, ports, stop, scratch)
        except Exception as error:
            log.exception("run %s failed in the engine", run_id)
            status, reason = "error", f"engine: {type(error).__name__}: {error}"
        finally:
            stop.set()
            self._close(run_id, state, held, ports)
            shutil.rmtree(scratch, ignore_errors=True)
            if state.shutdown and status in ("aborted", "error"):
                status, reason = "error", _SHUTDOWN_REASON
            try:
                state.writer.status(status, reason=reason)
            except Exception:
                log.exception("run %s: the terminal status could not be journaled", run_id)
            with self._lock:
                self._active.pop(run_id, None)
                self._states.pop(run_id, None)
            if active.done is not None:
                active.done.set()
            self._notify()
            self._dispatch()

    def _run(self, active: ActiveRun, state: _RunState, held_out: list[HeldResources],
             ports_out: list[DriverPorts], stop: threading.Event, scratch: Path) -> tuple[RunStatus, str | None]:
        admitted = active.admitted
        info = admitted.info
        run_id = info.id
        plan = admitted.plan
        entry = admitted.registry
        fakes = info.fakes
        writer = state.writer
        writer.status("preparing")
        if state.cancel.is_set():
            return "aborted", state.abort_reason

        def journal_event(type: str, detail: Mapping[str, JsonValue]) -> None:
            writer.event(type, detail=detail)

        seed_hook = FakeSeedHook() if fakes is not None and fakes.seeds else None
        cassette_hook = admitted.fakes.cassette_hook if admitted.fakes is not None else None
        try:
            held = self.planner.acquire(info, plan, admitted.resources, registry=entry.registry,
                                        sessions=admitted.sessions,
                                        sink=ResourceSink(record=writer.resource, event=journal_event),
                                        cancel=state.cancel, seed_mode=info.options.seed, seed_hook=seed_hook,
                                        cassette_hook=cassette_hook)
        except ResourceError as failure:
            if state.abort_reason is not None:
                return "aborted", state.abort_reason
            return "error", f"resource_error: {failure.message}"
        held_out.append(held)
        self._keepalive(run_id, held, stop)
        identities = {resource_id: handle.identity for resource_id, handle in held.resources.backends.items()}
        if identities:
            self.journal.set_environment(run_id, admitted.environment.model_copy(update={"backends": identities}))
        providers: dict[str, DriverProvider] = {}
        for platform in sorted(self.drivers.platforms()):
            provider = self.drivers.provider(platform)
            if provider is not None:
                providers[platform] = provider
        if fakes is not None and fakes.drivers:
            fake = FakeDriverProvider(fakes, entry.registry, scratch / "ports", cassette_hook=cassette_hook,
                                      plan=plan, backends=identities)
            for platform in sorted(FAKE_PLATFORMS):
                providers.setdefault(platform, fake)
        else:
            workers = self._worker_provider(info, plan, entry.registry, held, scratch / "workers")
            for platform in sorted(workers.platforms):
                providers.setdefault(platform, workers)
        driver_ports = DriverPorts(providers, info, admitted.sessions)
        ports_out.append(driver_ports)
        try:
            driver_ports.open_all(list(held.resources.leases.values()))
        except NodeFailure as failure:
            if state.abort_reason is not None:
                return "aborted", state.abort_reason
            return "error", f"{failure.code}: {failure.message}"
        options = info.options
        config = self.ctx.config
        settings = SchedulerConfig(
            parallelism=options.parallelism or int(config["engine.parallelism"]),
            retries=options.retries if options.retries is not None else int(config["engine.retries"]),
            node_timeout=options.node_timeout or float(config["engine.nodeTimeoutSeconds"]),
            pause_timeout=options.pause_timeout or float(config["engine.pauseTimeoutSeconds"]))
        debugger = Debugger(enabled=options.debug or bool(options.breakpoints) or options.break_on_error,
                            status=lambda: self._live_status(run_id), breakpoints=options.breakpoints,
                            break_on_error=options.break_on_error, journal=journal_event)
        fake_source = admitted.fakes
        executors = self.executors

        def choose(op: OpSpec) -> ExecutorChoice | None:
            try:
                return executors.choose(op, fakes=fakes, fake_source=fake_source)
            except ValueError:
                return None

        content = ContentResolver(self.artifacts, tree=Path(info.tree) if info.tree else None, base=plan.base,
                                  plugin_roots=entry.roots(), scratch=scratch / "content")
        deps = SchedulerDeps(journal=writer, cache=self._cache_service, content=content, plugins=entry,
                             artifacts=self.artifacts, resources=held.resources, slots=self.planner.slots,
                             drivers=driver_ports, sandbox=self._sandbox_runner, debugger=debugger, choose=choose,
                             digests={plugin_id: plugin.digest for plugin_id, plugin in entry.plugins.items()})
        scheduler = Scheduler(info, plan, entry.registry, admitted.executors, admitted.sessions, deps, settings)
        with self._lock:
            active.scheduler = scheduler
            abort = state.abort_reason
        if abort is not None:
            scheduler.abort(abort)
        writer.status("running")
        outcome = scheduler.run()
        if outcome.outputs is not None:
            self.journal.set_outputs(run_id, outcome.outputs)
        self.journal.set_counts(run_id, outcome.counts, outcome.cache)
        if self.seed_engine.building(run_id):
            if outcome.status != "passed":
                self.seed_engine.discard(run_id)
                return outcome.status, outcome.reason
            try:
                reports = self.seed_engine.finish(run_id)
            except SeedError as error:
                return "error", f"seed_error: {error.message}"
            for report in reports:
                writer.event(SEED_SNAPSHOT_EVENT, detail=report)
                if report.get("deterministic") is False:
                    writer.event(SEED_DIVERGED_EVENT, detail={"seed": report.get("seed"),
                                                              "divergence": report.get("divergence")})
        return outcome.status, outcome.reason

    def _worker_provider(self, info: RunInfo, plan: Plan, registry: Registry, held: HeldResources,
                         scratch: Path) -> WorkerDriverProvider:
        host = self.ctx.plugins
        conn = self.ctx.db.conn()
        hide = project_settings.union(conn, "capture.hideSelectors", [info.project])
        app_ids = {"ios": project_settings.union(conn, "apps.iosBundleIds", [info.project]),
                   "android": project_settings.union(conn, "apps.androidPackages", [info.project])}

        def extensions(platform: str, tree: Path | None) -> list[dict[str, JsonValue]]:
            return driver_extensions(host, platform, tree)

        artifacts = self.artifacts
        return WorkerDriverProvider(paths=self.ctx.paths, config=self.ctx.config, registry=registry,
                                    resources=held.resources, scratch=scratch, artifact_file=artifacts.file,
                                    extensions=extensions, hide=hide, plan=plan, cassettes=self.cassettes.hook(),
                                    devices=PoolDeviceCapture(self.ctx), app_ids=app_ids)

    def _keepalive(self, run_id: str, held: HeldResources, stop: threading.Event) -> None:
        if not held.resources.leases and not held.resources.backends:
            return

        def beat() -> None:
            while not stop.wait(KEEPALIVE_SECONDS):
                try:
                    self.planner.keepalive(held)
                except Exception:
                    log.exception("run %s: keepalive failed", run_id)

        threading.Thread(target=beat, name=f"eks-run-{run_id}-keepalive", daemon=True).start()

    def _close(self, run_id: str, state: _RunState, held: list[HeldResources], ports: list[DriverPorts]) -> None:
        for opened in ports:
            try:
                opened.close()
            except Exception:
                log.exception("run %s: driver ports could not be closed", run_id)
        writer = state.writer

        def journal_event(type: str, detail: Mapping[str, JsonValue]) -> None:
            writer.event(type, detail=detail)

        for resources in held:
            try:
                self.planner.release(resources, state.abort_reason or "the run ended",
                                     ResourceSink(record=writer.resource, event=journal_event))
            except Exception:
                log.exception("run %s: resources could not be released", run_id)
        self.seed_engine.discard(run_id)
        self.auth.forget(run_id)


def _check_breakpoints(breakpoints: list[str]) -> None:
    issues: list[IssueOut] = []
    for text in breakpoints:
        try:
            parse_breakpoint(text)
        except ValueError as problem:
            issues.append(IssueOut(severity="error", code="debug.breakpoint", message=str(problem)))
    if issues:
        raise AdmissionError(issues[0].message, issues)


def _debugger_state(row: RunRow) -> DebuggerState:
    options = row.info.options
    return "running" if options.debug or options.breakpoints or options.break_on_error else "off"


def engine(ctx: AppContext) -> RunManager:
    if not ctx.has_service(SERVICE):
        ctx.register_service(SERVICE, RunManager(ctx))
    return ctx.service(SERVICE)


def _start_engine(ctx: AppContext) -> None:
    manager = engine(ctx)
    recovered = manager.recover()
    if recovered:
        log.warning("marked %d runs that a hub restart interrupted as error", len(recovered))
    manager._start_maintenance()


def _stop_engine(ctx: AppContext) -> None:
    if ctx.has_service(SERVICE):
        engine(ctx).shutdown()


on_startup(_start_engine, order=45)
on_shutdown(_stop_engine, order=5)


__all__ = [
    "MAINTENANCE_SECONDS",
    "RECOVERY_REASON",
    "SERVICE",
    "ActiveRun",
    "RunManager",
    "engine",
]
