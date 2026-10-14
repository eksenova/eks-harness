from __future__ import annotations

import shutil
import sqlite3
import threading
import time
import traceback
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from eks.validation import validate as validate_plan
from eks_harness.engine.cache import (
    BlobRecord,
    Bypass,
    CacheEntry,
    CacheKey,
    CacheKeyRequest,
    CacheService,
    ContentResolver,
    PluginDigests,
    cache_key,
    gather_inputs,
    required_resolution,
    resolution_problems,
)
from eks_harness.engine.clock import run_time_context
from eks_harness.engine.debugger import (
    ControlTicket,
    Debugger,
    NodeProbe,
    PauseDecision,
)
from eks_harness.engine.drivers import DriverPort, DriverPorts, SnapshotKind
from eks_harness.engine.errors import NodeFailure, RefResolutionError
from eks_harness.engine.executors import (
    NATIVE_OPS,
    ArtifactSink,
    ArtifactStore,
    BlobRef,
    BodyOutcome,
    ContainerEntry,
    ExecContext,
    ExecResult,
    ExecutorChoice,
    ResolveContext,
    SessionState,
)
from eks_harness.engine.journal import RunJournal, bounded_detail
from eks_harness.engine.model import (
    FINAL_NODE_STATUSES,
    ROOT_INSTANCE,
    SETTLED_STATUSES,
    TAKE_OP,
    ArtifactRef,
    CacheCounts,
    ControlRequest,
    ControlResponse,
    NodeError,
    NodeRecord,
    NodeStatus,
    PauseInfo,
    ResourceRecord,
    RunInfo,
    RunResources,
    RunStatus,
    SlotHandle,
)
from eks_harness.engine.resources import SlotManager
from eks_harness.engine.sandbox import SandboxRunner, SubPlan
from eks_harness.engine.values import (
    ResolvedArgs,
    ValueStore,
    checked_output,
    child_instance,
    dynamic_segment,
    each_segment,
    instance_prefixes,
    instance_segments,
    output_problem,
    resolve,
    resolve_args,
    truthy,
    validate_args,
    value_digest,
)
from eks_harness.ir.hashing import (
    SOURCE_OP,
    clock_dependencies,
    derived_seed,
    machine_dependent,
    node_seed,
)
from eks_harness.ir.ops.check import ExpectValue
from eks_harness.ir.ops.ctl import Dynamic, Each, Macro, When
from eks_harness.ir.ops.debug import Break, PauseOnError
from eks_harness.ir.ops.score import Take
from eks_harness.ir.plan import Clock, Node, Plan, SessionSpec
from eks_harness.ir.refs import REF_KEY, RefError, RefExpr, walk_exprs
from eks_harness.ir.registry import OpSpec, Registry, using_registry
from eks_harness.ir.time import TICKS_PER_SECOND, Time, TimeContext
from eks_harness.ir.types import Artifact
from eks_harness.ops.expect import DEFAULT_EVENTUALLY_SECONDS

WAIT_SLICE_SECONDS = 0.5
TIMEOUT_GRACE_SECONDS = 5.0
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0)
DATA_EXTEND_OP = "data.extend"
DATA_CREATE_OP = "data.create"
DATA_CREATE_OPS = frozenset({DATA_CREATE_OP, "data.base"})
SEED_PHASES: tuple[str, ...] = ("schema", "api", "finalize", "mongo")


def _phase_rank(node: Node) -> int:
    phase = node.args.get("phase") or "api"
    return SEED_PHASES.index(phase) if phase in SEED_PHASES else SEED_PHASES.index("api")


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    parallelism: int = 4
    retries: int = 2
    node_timeout: float = 120.0
    pause_timeout: float = 1800.0
    cancel_grace: float = 10.0
    max_depth: int = 4


@dataclass
class SchedulerDeps:
    journal: RunJournal
    cache: CacheService
    content: ContentResolver
    plugins: PluginDigests
    artifacts: ArtifactStore
    resources: RunResources
    slots: SlotManager
    drivers: DriverPorts | None
    sandbox: SandboxRunner
    debugger: Debugger
    choose: Callable[[OpSpec], ExecutorChoice | None] | None = None
    digests: dict[str, str] = field(default_factory=dict[str, str])
    values: ValueStore = field(default_factory=ValueStore)
    states: dict[str, SessionState] = field(default_factory=dict[str, SessionState])


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunStatus
    reason: str | None
    outputs: JsonValue
    counts: Mapping[str, int]
    cache: CacheCounts


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    status: RunStatus
    counts: Mapping[str, int]
    running: tuple[str, ...]
    pause: PauseInfo | None
    cache: CacheCounts


_Phase = Literal["run", "enter", "exit", "dynamic", "native"]
_PauseKind = Literal["before", "error", "source"]
_TaskKind = Literal["run", "enter", "exit", "dynamic", "reread"]
_EndStatus = Literal["failed", "aborted", "error"]

_RENDER_LOCATIONS = frozenset({"node", "blender", "browser_scene"})
_NATIVE_CONTAINERS = frozenset({"ctl.when", "ctl.each", "ctl.parallel", "ctl.macro"})
_DYNAMIC_OP = "ctl.dynamic"
_BREAK_OP = "debug.break"
_PAUSE_ON_ERROR_OP = "debug.pause_on_error"
_EXPECT_VALUE_OP = "expect.value"
_LEASE_PREFIX = "lease."
_BACKEND_KIND = "backend"
_ARTIFACT_PREFIX = "artifact:"
_ADHOC_PREFIX = "adhoc:"
_TICKET_POLL_SECONDS = 0.05
_PUBLISH_SECONDS = 0.05
_MIN_WAIT_SECONDS = 0.01
_TRACEBACK_LINES = 20
_PAUSE_SNAPSHOTS: tuple[SnapshotKind, ...] = ("screen", "dom")
_FAILURE_SNAPSHOTS: tuple[SnapshotKind, ...] = ("screen",)
_TASK_PHASES: Mapping[_TaskKind, _Phase] = {"run": "run", "enter": "enter", "exit": "exit", "dynamic": "dynamic",
                                             "reread": "run"}
_CACHE_ERRORS = (OSError, ValueError, sqlite3.Error)


class _NodeSink:
    def __init__(self, journal: RunJournal, owner: _Instance) -> None:
        self.journal = journal
        self.owner = owner

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        owner = self.owner
        self.journal.event(type, node_id=owner.node.id, instance=owner.instance, attempt=owner.record.attempt,
                           detail=dict(detail))

    def progress(self, fraction: float, message: str) -> None:
        owner = self.owner
        self.journal.progress(owner.node.id, owner.instance, owner.record.attempt, fraction, message)

    def artifact(self, artifact: Artifact) -> None:
        owner = self.owner
        self.journal.event("node.artifact", node_id=owner.node.id, instance=owner.instance,
                           attempt=owner.record.attempt,
                           detail={"artifact": artifact.id, "kind": artifact.kind, "name": artifact.name})


@dataclass(eq=False)
class _Scope:
    owner: _Instance | None
    instance: str
    members: list[_Instance] = field(default_factory=list["_Instance"])

    @property
    def final(self) -> bool:
        return all(member.final for member in self.members)


@dataclass(eq=False)
class _Instance:
    node: Node
    instance: str
    plan: Plan
    spec: OpSpec | None
    choice: ExecutorChoice | None
    session: SessionSpec | None
    parent: _Instance | None
    scope: _Scope | None
    record: NodeRecord
    sink: _NodeSink | None = None
    artifacts: ArtifactSink | None = None
    resolved: ResolvedArgs | None = None
    edited: bool = False
    bodies: list[_Scope] = field(default_factory=list[_Scope])
    entry: ContainerEntry | None = None
    body: BodyOutcome | None = None
    items: list[JsonValue] = field(default_factory=list[JsonValue])
    iteration: int = 0
    subplan: Plan | None = None
    inputs: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    retries: int = 0
    retry_at: float | None = None
    content: str | None = None
    key: CacheKey | None = None
    predicted: dict[str, JsonValue] | None = None
    produced_from: int = 0
    token: int = 0
    adhoc: bool = False

    @property
    def final(self) -> bool:
        return self.record.status in FINAL_NODE_STATUSES

    @property
    def name(self) -> str:
        return self.node.id if self.instance == ROOT_INSTANCE else f"{self.node.id}@{self.instance}"

    def sink_of(self) -> _NodeSink:
        if self.sink is None:
            raise RuntimeError(f"node instance {self.name} has no sink")
        return self.sink

    def store_of(self) -> ArtifactSink:
        if self.artifacts is None:
            raise RuntimeError(f"node instance {self.name} has no artifact sink")
        return self.artifacts


@dataclass(eq=False)
class _Task:
    kind: _TaskKind
    token: int
    deadline: float
    seconds: float
    leases: tuple[str, ...]
    prepare: bool = False
    waiting: bool = False


@dataclass(frozen=True, slots=True)
class _Queued:
    owner: _Instance
    kind: _TaskKind
    prepare: bool


@dataclass(frozen=True, slots=True)
class _Outcome:
    result: ExecResult | None = None
    value: JsonValue = None
    ids: tuple[str, ...] = ()
    hit: bool = False
    entry: ContainerEntry | None = None
    subplan: SubPlan | None = None
    simulated: bool = False


@dataclass(eq=False)
class _Completion:
    owner: _Instance
    task: _Task
    outcome: _Outcome | None
    error: NodeFailure | None


@dataclass(eq=False)
class _Pause:
    owner: _Instance
    kind: _PauseKind
    phase: _Phase
    decision: PauseDecision
    error: NodeError | None
    started: float = 0.0
    info: PauseInfo | None = None


def _unexpected(error: BaseException, code: str = "executor_error") -> NodeFailure:
    lines = "".join(traceback.format_exception(error)).splitlines()[-_TRACEBACK_LINES:]
    return NodeFailure(code, f"{type(error).__name__}: {error}",
                       detail={"type": type(error).__name__, "message": str(error),
                               "traceback": cast("list[JsonValue]", lines)})


def _guard[T](work: Callable[[], T], code: str = "executor_error") -> T:
    try:
        return work()
    except NodeFailure:
        raise
    except Exception as problem:
        raise _unexpected(problem, code) from problem


def _dedupe(ids: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _dotted(record: Mapping[str, JsonValue] | None, path: str) -> JsonValue:
    if record is None:
        return None
    if path in record:
        return record[path]
    value: JsonValue = cast("JsonValue", dict(record))
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _normalized(value: JsonValue, shas: Mapping[str, str]) -> JsonValue:
    if isinstance(value, str):
        if value in shas:
            return shas[value]
        if value.startswith(_ARTIFACT_PREFIX) and value[len(_ARTIFACT_PREFIX):] in shas:
            return _ARTIFACT_PREFIX + shas[value[len(_ARTIFACT_PREFIX):]]
        return value
    if isinstance(value, list):
        return [_normalized(item, shas) for item in value]
    if isinstance(value, dict):
        ident = value.get("id")
        if isinstance(ident, str) and ident in shas and "kind" in value:
            return {"artifact": shas[ident]}
        return {key: _normalized(item, shas) for key, item in value.items()}
    return value


def _seconds(duration: object) -> float | None:
    if isinstance(duration, Time) and duration.is_concrete:
        return duration.ticks / TICKS_PER_SECOND
    return None


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


class Scheduler:
    def __init__(self, run: RunInfo, plan: Plan, registry: Registry, executors: Mapping[str, ExecutorChoice],
                 sessions: Mapping[str, SessionSpec], deps: SchedulerDeps, config: SchedulerConfig) -> None:
        self.run_info = run
        self.plan = plan
        self.registry = registry
        self.executors = dict(executors)
        self.sessions = dict(sessions)
        self.deps = deps
        self.config = config
        self.cancel = threading.Event()
        self._lock = threading.Condition()
        self.deps.content.cancel = self.cancel
        self._pool: ThreadPoolExecutor | None = None
        self._status: RunStatus = "running"
        self._seq = 0
        self._adhoc_count = 0
        self._instances: dict[tuple[str, str], _Instance] = {}
        self._order: list[_Instance] = []
        self._seed_positions: dict[str, int] | None = None
        self._clock_providers: dict[str, tuple[str, ...]] | None = None
        self._pending: list[_Instance] = []
        self._inflight: dict[_Instance, _Task] = {}
        self._abandoned: dict[_Task, _Instance] = {}
        self._queued: list[_Queued] = []
        self._tickets: dict[_Instance, ControlTicket] = {}
        self._busy: dict[str, _Task] = {}
        self._retrying: list[_Instance] = []
        self._done: deque[_Completion] = deque()
        self._root: _Scope | None = None
        self._pause: _Pause | None = None
        self._pauses: deque[_Pause] = deque()
        self._ending = False
        self._end_status: _EndStatus = "failed"
        self._ending_since = 0.0
        self._failure: str | None = None
        self._abort_lock = threading.Lock()
        self._abort_reason: str | None = None
        self._error: str | None = None
        self._crashes = 0
        self._finished = False
        self._cache_counts = CacheCounts()
        self._sub_choices: dict[str, ExecutorChoice | None] = {}
        self._kinds: dict[int, dict[str, str]] = {}
        self._published = self._fresh_status()
        self._published_at = time.monotonic()

    def run(self) -> RunOutcome:
        ended = "with an engine error"
        pool = ThreadPoolExecutor(max_workers=self._capacity(), thread_name_prefix=f"eks-run-{self.run_info.id}")
        self._pool = pool
        try:
            with self._lock:
                try:
                    _guard(self._start, "engine_error")
                except NodeFailure as failure:
                    self._crash(failure)
                self._publish(force=True)
            while True:
                ticket: ControlTicket | None = None
                if self._serving():
                    ticket = self.deps.debugger.next_ticket(self.cancel, timeout=_TICKET_POLL_SECONDS)
                with self._lock:
                    try:
                        changed, outcome = _guard(partial(self._step, ticket), "engine_error")
                    except NodeFailure as failure:
                        outcome = self._crash(failure)
                        changed = True
                    if outcome is not None:
                        self._status = outcome.status
                        self._finished = True
                        ended = outcome.status
                        self._publish(force=True)
                        return outcome
                    self._publish()
                    if not changed and (self._pause is None or self._ending):
                        self._lock.wait(self._wait_seconds())
        except BaseException:
            self.cancel.set()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            self.deps.debugger.close(f"the run ended {ended}")

    def control(self, request: ControlRequest, timeout: float = 30.0) -> ControlResponse:
        response = self.deps.debugger.submit(request, timeout)
        with self._lock:
            self._lock.notify_all()
        return response

    def abort(self, reason: str) -> None:
        self._set_abort(reason)
        self.cancel.set()
        self.deps.debugger.request_abort()
        with self._lock:
            self._lock.notify_all()

    def status(self) -> SchedulerStatus:
        if not self._lock.acquire(blocking=False):
            return self._published
        try:
            return self._fresh_status()
        finally:
            self._lock.release()

    def _fresh_status(self) -> SchedulerStatus:
        running = tuple(owner.name for owner in self._order if owner.record.status == "running")
        return SchedulerStatus(status=self._status, counts=self._counts(), running=running,
                               pause=self._pause.info if self._pause is not None else None,
                               cache=self._cache_counts.model_copy())

    def _publish(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._published_at >= _PUBLISH_SECONDS:
            self._published = self._fresh_status()
            self._published_at = now

    def _set_abort(self, reason: str) -> None:
        with self._abort_lock:
            if self._abort_reason is None and not self._finished:
                self._abort_reason = reason

    def _start(self) -> None:
        self._root = self._activate(None, self.plan.nodes, ROOT_INSTANCE, self.plan, self.plan.session)

    def _step(self, ticket: ControlTicket | None) -> tuple[bool, RunOutcome | None]:
        changed = False
        if ticket is not None:
            self._serve(ticket)
            changed = True
        changed = self._advance() or changed
        return changed, self._outcome(changed)

    def _crash(self, failure: NodeFailure) -> RunOutcome | None:
        self._crashes += 1
        if self._error is None:
            self._error = f"engine: {failure.message}"
        if self._crashes > 1:
            self.cancel.set()
            return RunOutcome(status="error", reason=self._error, outputs=None, counts=self._counts(),
                              cache=self._cache_counts.model_copy())
        if self._ending:
            self._end_status = "error"
        else:
            self._begin_ending("error")
        return None

    def _serving(self) -> bool:
        with self._lock:
            return self._pause is not None and not self._ending

    def _counts(self) -> dict[str, int]:
        return dict(Counter(owner.record.status for owner in self._order))

    def _capacity(self) -> int:
        return max(1, self.config.parallelism)

    def _load(self) -> int:
        return len(self._inflight) + len(self._abandoned)

    def _wait_seconds(self) -> float:
        now = time.monotonic()
        wait = WAIT_SLICE_SECONDS
        for owner in self._retrying:
            if owner.retry_at is not None and owner.retry_at > now:
                wait = min(wait, owner.retry_at - now)
        for task in self._inflight.values():
            if not task.waiting:
                wait = min(wait, task.deadline + TIMEOUT_GRACE_SECONDS - now)
        if self._ending:
            wait = min(wait, self._ending_since + self.config.cancel_grace - now)
        return max(_MIN_WAIT_SECONDS, wait)

    def _check_abort(self) -> bool:
        if self._ending:
            return False
        if self._abort_reason is None and self.deps.debugger.abort_requested:
            self._set_abort("aborted from the debugger")
        if self._abort_reason is None:
            return False
        self._begin_ending("aborted")
        return True

    def _advance(self) -> bool:
        changed = self._check_abort()
        while self._done:
            self._apply(self._done.popleft())
            changed = True
        changed = self._check_abort() or changed
        changed = self._timers() or changed
        if self._ending:
            return changed
        changed = self._dispatch() or changed
        if self._pause is not None:
            if time.monotonic() - self._pause.started > self.config.pause_timeout:
                self._set_abort(f"paused longer than {self.config.pause_timeout:g} s")
                self._begin_ending("aborted")
                return True
            return changed
        if self._pauses:
            self._enter_pause(self._pauses.popleft())
            return True
        return self._start_ready() or changed

    def _outcome(self, changed: bool) -> RunOutcome | None:
        if self._ending:
            if self._inflight and time.monotonic() - self._ending_since < self.config.cancel_grace:
                return None
            self._finalize()
            reasons = {"failed": self._failure, "aborted": self._abort_reason, "error": self._error}
            return RunOutcome(status=self._end_status, reason=reasons[self._end_status], outputs=None,
                              counts=self._counts(), cache=self._cache_counts.model_copy())
        root = self._root
        if root is not None and root.final and not self._inflight and not self._queued:
            return RunOutcome(status="passed", reason=None, outputs=self._outputs(), counts=self._counts(),
                              cache=self._cache_counts.model_copy())
        if (changed or self._pause is not None or self._pauses or self._inflight or self._abandoned or self._queued
                or self._retrying or self._done):
            return None
        self._error = self._deadlock()
        self._begin_ending("error")
        return self._outcome(True)

    def _deadlock(self) -> str:
        for owner in self._pending:
            waiting = self._unsettled(owner)
            if waiting:
                return f"engine: deadlock at {owner.node.id} waiting for {', '.join(waiting)}"
        if self._pending:
            return f"engine: deadlock at {self._pending[0].node.id} waiting for a free lease or the seed extends"
        for owner in self._order:
            if owner.record.status == "running":
                return f"engine: deadlock at {owner.node.id} waiting for its body"
        return "engine: deadlock: the root scope never settled"

    def _outputs(self) -> JsonValue:
        try:
            return resolve(self.plan.outputs, self.deps.values, ROOT_INSTANCE)
        except RefResolutionError as error:
            self.deps.journal.event("run.outputs_unresolved", detail={"message": error.message,
                                                                      "detail": error.detail})
            return None

    def _begin_ending(self, status: _EndStatus) -> None:
        if self._ending:
            return
        self._ending = True
        self._end_status = status
        self._ending_since = time.monotonic()
        self.cancel.set()
        self._lock.notify_all()

    def _finalize(self) -> None:
        for owner, task in list(self._inflight.items()):
            self._free(task)
            self._cancelled(owner, "the run ended while the node ran")
        self._inflight.clear()
        for task in list(self._abandoned):
            self._free(task)
        self._abandoned.clear()
        self._queued.clear()
        for owner in list(self._retrying):
            self._cancelled(owner, "the run ended before the retry")
        self._retrying.clear()
        pauses = ([self._pause] if self._pause is not None else []) + list(self._pauses)
        self._pause = None
        self._pauses.clear()
        for pause in pauses:
            if pause.kind == "error" and pause.error is not None:
                self._mark_failed(pause.owner, pause.error)
            else:
                self._cancelled(pause.owner, "the run ended while it was paused here")
        self._pending.clear()
        for owner in sorted(self._order, key=lambda candidate: candidate.record.seq, reverse=True):
            if owner.final:
                continue
            failed = self._body_failure(owner)
            if failed is not None:
                self._mark_failed(owner, self._body_error(failed))
            else:
                self._cancelled(owner, "the run ended before the node finished")
        for owner in list(self._tickets):
            self._answer_adhoc(owner, False, f"the run ended before {owner.node.op} {owner.node.id} finished")

    def _body_failure(self, owner: _Instance) -> _Instance | None:
        failed = [member for scope in owner.bodies for member in scope.members if member.record.status == "failed"]
        return min(failed, key=lambda member: member.record.seq) if failed else None

    def _body_error(self, failed: _Instance) -> NodeError:
        inner = failed.record.error
        return NodeError(code=inner.code if inner is not None else "executor_error",
                         message=f"body failed at {failed.node.id}",
                         detail={"node": failed.node.id, "instance": failed.instance,
                                 "message": inner.message if inner is not None else ""})

    def _kinds_of(self, plan: Plan) -> dict[str, str]:
        found = self._kinds.get(id(plan))
        if found is None:
            found = {resource.id: resource.kind for resource in plan.resources}
            self._kinds[id(plan)] = found
        return found

    def _leases(self, owner: _Instance) -> tuple[str, ...]:
        kinds = self._kinds_of(owner.plan)
        return tuple(rid for rid in owner.node.uses if kinds.get(rid, "").startswith(_LEASE_PREFIX))

    def _backends(self, owner: _Instance) -> tuple[str, ...]:
        kinds = self._kinds_of(owner.plan)
        return tuple(rid for rid in owner.node.uses if kinds.get(rid) == _BACKEND_KIND)

    def _visible(self, node_id: str, instance: str) -> _Instance | None:
        for prefix in instance_prefixes(instance):
            found = self._instances.get((node_id, prefix))
            if found is not None:
                return found
        return None

    def _unsettled(self, owner: _Instance) -> list[str]:
        waiting: list[str] = []
        for dependency in owner.node.after:
            found = self._visible(dependency, owner.instance)
            if found is None or found.record.status not in SETTLED_STATUSES:
                waiting.append(dependency)
        return waiting

    def _seed_blocked(self, owner: _Instance) -> bool:
        if self.plan.kind != "seed" or owner.node.op not in DATA_CREATE_OPS:
            return False
        if any(candidate.node.op == DATA_EXTEND_OP and candidate.record.status not in SETTLED_STATUSES
               for candidate in self._order):
            return True
        return self._phase_blocked(owner)

    def _phase_blocked(self, owner: _Instance) -> bool:
        mine = self._seed_rank(owner.node)
        for node in self.plan.nodes:
            if node.op not in DATA_CREATE_OPS or node.id == owner.node.id or self._seed_rank(node) >= mine:
                continue
            found = self._visible(node.id, owner.instance)
            if found is None or found.record.status not in SETTLED_STATUSES:
                return True
        return False

    def _seed_rank(self, node: Node) -> tuple[int, int]:
        if self._seed_positions is None:
            self._seed_positions = {item.id: index for index, item in enumerate(self.plan.nodes)}
        phase = _phase_rank(node) if node.op == DATA_CREATE_OP else -1
        return phase, self._seed_positions.get(node.id, 0)

    def _takes_lease(self, owner: _Instance) -> bool:
        return owner.node.op not in NATIVE_OPS and owner.spec is not None

    def _lease_free(self, owner: _Instance) -> bool:
        if not self._takes_lease(owner):
            return True
        return self._leases_free(self._leases(owner))

    def _leases_free(self, leases: Sequence[str]) -> bool:
        return all(rid not in self._busy for rid in leases)

    def _task_leases(self, owner: _Instance, kind: _TaskKind) -> tuple[str, ...]:
        return () if kind == "dynamic" else self._leases(owner)

    def _can_submit(self, owner: _Instance, kind: _TaskKind) -> bool:
        return self._load() < self._capacity() and self._leases_free(self._task_leases(owner, kind))

    def _queue(self, owner: _Instance, kind: _TaskKind, prepare: bool = False) -> None:
        if self._can_submit(owner, kind):
            self._submit(owner, kind, prepare)
            return
        self._queued.append(_Queued(owner=owner, kind=kind, prepare=prepare))

    def _dispatch(self) -> bool:
        changed = False
        for queued in list(self._queued):
            if self._load() >= self._capacity():
                break
            if not self._can_submit(queued.owner, queued.kind):
                continue
            self._queued.remove(queued)
            self._submit(queued.owner, queued.kind, queued.prepare)
            changed = True
        return changed

    def _ready(self, owner: _Instance) -> bool:
        return not self._unsettled(owner) and not self._seed_blocked(owner) and self._lease_free(owner)

    def _start_ready(self) -> bool:
        started = False
        for owner in list(self._pending):
            if self._ending or self._pause is not None or self._pauses:
                break
            if self._load() + len(self._queued) >= self._capacity():
                break
            if not self._ready(owner):
                continue
            self._pending.remove(owner)
            self._begin(owner)
            started = True
        return started

    def _choice(self, node: Node, plan: Plan, spec: OpSpec | None) -> ExecutorChoice | None:
        if plan is self.plan or spec is None:
            return self.executors.get(node.op)
        return self._sub_choice(spec)

    def _sub_choice(self, spec: OpSpec) -> ExecutorChoice | None:
        if spec.name in self._sub_choices:
            return self._sub_choices[spec.name]
        choose = self.deps.choose
        chosen = choose(spec) if choose is not None else self.executors.get(spec.name)
        self._sub_choices[spec.name] = chosen
        return chosen

    def _create(self, node: Node, instance: str, plan: Plan, session: SessionSpec | None, parent: _Instance | None,
                scope: _Scope | None) -> _Instance:
        spec = self.registry.op(node.op) if self.registry.has_op(node.op) else None
        choice = self._choice(node, plan, spec)
        self._seq += 1
        record = NodeRecord(
            run_id=self.run_info.id, node_id=node.id, instance=instance,
            parent_id=parent.node.id if parent is not None else None,
            parent_instance=parent.instance if parent is not None else None,
            seq=self._seq, op=node.op, node_hash=node.hash, label=node.label, src=node.src, status="pending",
            executor=choice.executor.identity if choice is not None else None,
            location=spec.location if spec is not None else None)
        created = _Instance(node=node, instance=instance, plan=plan, spec=spec, choice=choice, session=session,
                            parent=parent, scope=scope, record=record)
        created.sink = _NodeSink(self.deps.journal, created)
        created.artifacts = ArtifactSink(self.deps.artifacts, run=self.run_info, node_id=node.id, instance=instance,
                                         op=node.op, sink=created.sink)
        self._instances[(node.id, instance)] = created
        self._order.append(created)
        self._save(created)
        return created

    def _activate(self, owner: _Instance | None, nodes: Sequence[Node], instance: str, plan: Plan,
                  session: SessionSpec | None) -> _Scope:
        scope = _Scope(owner=owner, instance=instance)
        if owner is not None:
            owner.bodies.append(scope)
        for node in nodes:
            member = self._create(node, instance, plan, session, owner, scope)
            scope.members.append(member)
            self._pending.append(member)
        if owner is not None and not scope.members:
            self._scope_done(scope)
        return scope

    def _skip_nodes(self, nodes: Sequence[Node], instance: str, parent: _Instance, reason: str) -> None:
        for node in nodes:
            if (node.id, instance) in self._instances:
                continue
            skipped = self._create(node, instance, parent.plan, parent.session, parent, None)
            skipped.record.status = "skipped"
            skipped.record.finished_at = time.time()
            self._save(skipped)
            self._event(skipped, "node.skipped", {"reason": reason})
            for branch in (node.branches or {}).values():
                self._skip_nodes(branch, instance, skipped, reason)

    def _save(self, owner: _Instance) -> None:
        self.deps.journal.node(owner.record)

    def _event(self, owner: _Instance, type: str, detail: Mapping[str, JsonValue] | None = None) -> None:
        self.deps.journal.event(type, node_id=owner.node.id, instance=owner.instance, attempt=owner.record.attempt,
                                detail=dict(detail or {}))

    def _resolution(self, owner: _Instance) -> dict[str, JsonValue]:
        if owner.record.resolution is None:
            owner.record.resolution = {}
        return owner.record.resolution

    def _probe(self, owner: _Instance, error: NodeError | None = None) -> NodeProbe:
        record = owner.record
        return NodeProbe(node_id=owner.node.id, instance=owner.instance, attempt=record.attempt, op=owner.node.op,
                         label=owner.node.label, src=owner.node.src, args=record.args,
                         session=owner.session.key if owner.session is not None else None, error=error)

    def _spec(self, owner: _Instance) -> OpSpec:
        if owner.spec is None:
            raise NodeFailure("executor_missing",
                              f"op {owner.node.op} of node {owner.node.id} is not registered on this hub")
        return owner.spec

    def _resolved(self, owner: _Instance) -> ResolvedArgs:
        if owner.resolved is None:
            raise NodeFailure("args_invalid", f"{owner.node.op} {owner.node.id} has no resolved arguments")
        return owner.resolved

    def _resolve_args(self, owner: _Instance) -> ResolvedArgs:
        spec = self._spec(owner)
        try:
            with using_registry(self.registry):
                return resolve_args(owner.node, spec, self.deps.values, owner.instance)
        except RefResolutionError as error:
            raise self._explained(owner, error) from error

    def _explained(self, owner: _Instance, error: RefResolutionError) -> RefResolutionError:
        try:
            exprs = list(walk_exprs(owner.node.args))
        except RefError:
            return error
        for expr in exprs:
            if not isinstance(expr, RefExpr):
                continue
            producer = self._visible(expr.ref, owner.instance)
            if (producer is not None and producer.record.status == "soft_failed"
                    and not self.deps.values.has(expr.ref, producer.instance)):
                return RefResolutionError(
                    f"{owner.node.op} {owner.node.id}: node {expr.ref} soft-failed and has no output",
                    detail={**error.detail, "producer": expr.ref, "instance": producer.instance})
        return error

    def _begin(self, owner: _Instance) -> None:
        record = owner.record
        record.status = "ready"
        record.queued_at = time.time()
        self._event(owner, "node.ready")
        record.status = "running"
        record.attempt += 1
        record.started_at = time.time()
        record.finished_at = None
        record.duration_ms = None
        self._save(owner)
        self._event(owner, "node.started", {"attempt": record.attempt})
        if owner.choice is None:
            self._fail(owner, NodeFailure(
                "executor_missing", f"no executor for {owner.node.op} on this hub (node {owner.node.id})"), "run")
            return
        if not owner.edited:
            try:
                owner.resolved = self._resolve_args(owner)
            except NodeFailure as failure:
                self._fail(owner, failure, "run")
                return
        record.args = self._resolved(owner).data
        for rid in self._backends(owner):
            handle = self.deps.resources.backend(rid)
            if handle is not None:
                self._resolution(owner)["backend"] = cast("JsonValue", handle.identity.model_dump(mode="json"))
                break
        self._save(owner)
        decision = self.deps.debugger.before_node(self._probe(owner), self.deps.values.snapshot())
        if decision is not None:
            self._pause_at(owner, "before", "run", decision, None)
            return
        self._proceed(owner)

    def _proceed(self, owner: _Instance) -> None:
        op = owner.node.op
        try:
            if owner.resolved is None:
                owner.resolved = self._resolve_args(owner)
                owner.record.args = owner.resolved.data
            if op == _BREAK_OP:
                self._debug_break(owner)
            elif op == _PAUSE_ON_ERROR_OP:
                self._pause_on_error(owner)
            elif op in _NATIVE_CONTAINERS:
                self._native(owner)
            elif op == _DYNAMIC_OP:
                self._submit_dynamic(owner)
            elif self._spec(owner).container:
                self._queue(owner, "enter")
            else:
                self._leaf(owner)
        except NodeFailure as failure:
            phase: _Phase = "dynamic" if op == _DYNAMIC_OP else ("native" if op in NATIVE_OPS else "run")
            self._fail(owner, failure, phase)

    def _debug_break(self, owner: _Instance) -> None:
        model = self._resolved(owner).model
        label = model.label if isinstance(model, Break) else owner.node.label or owner.node.id
        hit = truthy(cast("JsonValue", model.when)) if isinstance(model, Break) and "when" in owner.node.args else True
        decision = self.deps.debugger.at_source(self._probe(owner), label, hit, self.deps.values.snapshot())
        if decision is not None:
            self._pause_at(owner, "source", "native", decision, None)
            return
        self._pass(owner, None, detail={"hit": hit, "label": label})

    def _pause_on_error(self, owner: _Instance) -> None:
        model = self._resolved(owner).model
        on = model.on if isinstance(model, PauseOnError) else True
        self.deps.debugger.set_pause_on_error(owner.session.key if owner.session is not None else None, on)
        self._pass(owner, None, detail={"on": on})

    def _native(self, owner: _Instance) -> None:
        model = self._resolved(owner).model
        op = owner.node.op
        branches = owner.node.branches or {}
        if op == "ctl.when":
            cond = cast("JsonValue", model.cond) if isinstance(model, When) else None
            chosen: str | None = "then" if truthy(cond) else ("else" if "else" in branches else None)
            self.deps.journal.expansion(owner.node.id, owner.instance, "branch", {"branch": chosen})
            for name, nodes in branches.items():
                if name != chosen:
                    self._skip_nodes(nodes, owner.instance, owner, f"branch {name} not taken")
            self._activate(owner, branches.get(chosen, []) if chosen is not None else [], owner.instance,
                           owner.plan, owner.session)
            return
        if op == "ctl.each":
            over = cast("JsonValue", model.over) if isinstance(model, Each) else None
            if not isinstance(over, list):
                raise NodeFailure("each_not_list",
                                  f"ctl.each {owner.node.id} iterates over a {_json_type(over)}, not a list",
                                  detail={"type": _json_type(over)})
            limit = model.limit if isinstance(model, Each) else None
            items = list(over)[:limit] if limit is not None else list(over)
            self.deps.values.set_items(owner.node.id, owner.instance, items)
            self.deps.journal.expansion(owner.node.id, owner.instance, "each",
                                        bounded_detail({"count": len(items), "items": items}))
            owner.items = items
            owner.iteration = 0
            if not items:
                self._activate(owner, [], owner.instance, owner.plan, owner.session)
                return
            self._next_iteration(owner)
            return
        self._activate(owner, branches.get("body", []), owner.instance, owner.plan, owner.session)

    def _next_iteration(self, owner: _Instance) -> None:
        instance = child_instance(owner.instance, each_segment(owner.node.id, owner.iteration))
        body = (owner.node.branches or {}).get("body", [])
        self._activate(owner, body, instance, owner.plan, owner.session)

    def _depth(self, owner: _Instance) -> int:
        return 1 + sum(1 for segment in instance_segments(owner.instance) if segment.index is None)

    def _in_take(self, owner: _Instance) -> bool:
        current = owner.parent
        while current is not None:
            if current.node.op == TAKE_OP:
                return True
            current = current.parent
        return False

    def _submit_dynamic(self, owner: _Instance) -> None:
        depth = self._depth(owner)
        if depth > self.config.max_depth:
            raise NodeFailure("subplan_invalid",
                              f"ctl.dynamic {owner.node.id} is nested {depth} levels deep, more than the limit of "
                              f"{self.config.max_depth}",
                              detail={"depth": depth, "max_depth": self.config.max_depth})
        model = self._resolved(owner).model
        owner.inputs = cast("dict[str, JsonValue]", dict(model.inputs)) if isinstance(model, Dynamic) else {}
        self._queue(owner, "dynamic")

    def _leaf(self, owner: _Instance) -> None:
        self._spec(owner)
        self._queue(owner, "run", True)

    def _ensure_current(self, owner: _Instance, task: _Task) -> None:
        if self._inflight.get(owner) is not task:
            raise NodeFailure("cancelled", f"{owner.node.op} {owner.node.id} was abandoned, or the run ended, before "
                                           "its call finished")

    def _prepare(self, owner: _Instance, task: _Task) -> _Outcome | None:
        if owner.node.op == SOURCE_OP:
            self._content_check(owner, task)
        if owner.adhoc or not self._spec(owner).pure:
            return None
        return self._cache_before(owner, task)

    def _content_check(self, owner: _Instance, task: _Task) -> None:
        uri = self._resolved(owner).data.get("uri")
        if not isinstance(uri, str) or not uri:
            raise NodeFailure("args_invalid", f"media.source {owner.node.id} has no URI")
        media = self.deps.content.media(uri)
        with self._lock:
            self._ensure_current(owner, task)
            self._resolution(owner)["content"] = {"uri": media.uri, "sha256": media.sha256, "size": media.size,
                                                  "source": media.source}
            self._save(owner)

    def _predict(self, owner: _Instance) -> dict[str, JsonValue] | None:
        choice = owner.choice
        if choice is None:
            return None
        resolver = choice.executor.resolver
        if resolver is None:
            return None
        with using_registry(self.registry):
            machine = machine_dependent(owner.node, self.registry)
        if not machine:
            return None
        resolved = self._resolved(owner)
        context = ResolveContext(run=self.run_info, plan=owner.plan, registry=self.registry, node=owner.node,
                                 spec=self._spec(owner), args=resolved.model, data=resolved.data,
                                 resources=self.deps.resources)
        try:
            with using_registry(self.registry):
                found = dict(resolver(context))
        except NodeFailure:
            raise
        except Exception as error:
            raise NodeFailure("resolution_unavailable",
                              f"the resolver of {choice.executor.identity} failed for {owner.node.op} "
                              f"{owner.node.id}: {type(error).__name__}: {error}") from error
        problems = resolution_problems(owner.node, self.registry, found)
        if problems:
            raise NodeFailure("resolution_incomplete",
                              f"{owner.node.op} {owner.node.id}: the resolver of {choice.executor.identity} did not "
                              f"report {', '.join(problems)}",
                              detail={"missing": cast("list[JsonValue]", problems),
                                      "resolution": cast("JsonValue", found)})
        return found

    def _cache_before(self, owner: _Instance, task: _Task) -> _Outcome | None:
        record = owner.record
        mode = self.run_info.options.cache
        predicted = self._predict(owner)
        with self._lock:
            self._ensure_current(owner, task)
            if predicted is not None:
                owner.predicted = predicted
                self._resolution(owner)["machine"] = cast("JsonValue", predicted)
                self._save(owner)
                self._event(owner, "node.resolution", {"resolution": cast("JsonValue", predicted)})
            if mode == "off":
                record.cache = "off"
                return None
            if owner.edited:
                self._bypass(owner, "the arguments were edited in the debugger")
                return None
        executor = cast("ExecutorChoice", owner.choice).executor
        store = self.deps.values
        instance = owner.instance

        def override(node_id: str) -> str | None:
            with self._lock:
                producer = self._visible(node_id, instance)
                return producer.content if producer is not None else None

        try:
            with using_registry(self.registry):
                inputs = gather_inputs(owner.plan, owner.node, self.registry,
                                       value_of=lambda node_id: store.get(node_id, instance),
                                       content=self.deps.content, artifacts=self.deps.artifacts, override=override,
                                       item_of=lambda each: store.item(each, instance))
                key = cache_key(CacheKeyRequest(plan=owner.plan, node=owner.node, registry=self.registry,
                                                executor=executor, inputs=inputs, resolution=predicted,
                                                plugins=self.deps.plugins, clock=self.run_info.clock))
        except (ValueError, KeyError) as error:
            with self._lock:
                self._ensure_current(owner, task)
                self._bypass(owner, str(error))
            return None
        with self._lock:
            self._ensure_current(owner, task)
            if isinstance(key, Bypass):
                self._bypass(owner, key.reason)
                return None
            owner.key = key
            record.cache_key = key.key
        if mode not in ("use", "readonly"):
            return None
        problem: str | None = None
        try:
            hit = self.deps.cache.lookup(key, artifacts=self.deps.artifacts, materialize=self._materializer(owner))
        except _CACHE_ERRORS as error:
            hit = None
            problem = f"the cache entry could not be served: {type(error).__name__}: {error}"
        if hit is None:
            with self._lock:
                self._ensure_current(owner, task)
                if problem is not None:
                    self._event(owner, "node.log", {"level": "warning", "message": problem})
                self._cache_counts.misses += 1
                record.cache = "miss"
            return None
        value = checked_output(owner.node, hit.value)
        simulated = self._simulated(owner)
        with self._lock:
            self._ensure_current(owner, task)
            self._cache_counts.hits += 1
            record.cache = "hit"
            self._event(owner, "node.cache", {"outcome": "hit", "key": key.key, "rematerialized": hit.rematerialized,
                                              "artifacts": cast("list[JsonValue]", list(hit.artifacts))})
        return _Outcome(value=value, ids=tuple(hit.artifacts), hit=True, simulated=simulated)

    def _bypass(self, owner: _Instance, reason: str) -> None:
        self._cache_counts.bypassed += 1
        owner.record.cache = "bypass"
        self._event(owner, "node.cache", {"outcome": "bypass", "key": None, "reason": reason})

    def _materializer(self, owner: _Instance) -> Callable[[BlobRecord, Path], Artifact]:
        def materialize(blob: BlobRecord, path: Path) -> Artifact:
            directory = self.deps.artifacts.scratch()
            try:
                name = Path(blob.name).name or path.name
                copy = directory / name
                shutil.copyfile(path, copy)
                return owner.store_of().ingest(copy, kind=blob.kind, name=name, tags=["cache"], move=True)
            finally:
                shutil.rmtree(directory, ignore_errors=True)

        return materialize

    def _cache_after(self, owner: _Instance, task: _Task, value: JsonValue, ids: Sequence[str], soft: bool) -> None:
        key = owner.key
        if key is None:
            return
        record = owner.record
        mode = self.run_info.options.cache
        if mode not in ("use", "refresh") or soft:
            with self._lock:
                self._ensure_current(owner, task)
                record.cache = "miss"
                self._event(owner, "node.cache", {"outcome": "miss", "key": key.key})
            return
        with self._lock:
            self._ensure_current(owner, task)
        try:
            blobs = self._blobs(ids)
            previous = self.deps.cache.entry(key.key)
            self.deps.cache.store(key, node=owner.node, value=value, blobs=blobs, resolution=owner.predicted,
                                  run_id=self.run_info.id, artifacts=list(ids))
        except _CACHE_ERRORS as error:
            with self._lock:
                self._ensure_current(owner, task)
                record.cache = "miss"
                self._event(owner, "node.cache", {"outcome": "miss", "key": key.key,
                                                  "reason": f"{type(error).__name__}: {error}"})
            return
        detail: dict[str, JsonValue] = {"outcome": "stored", "key": key.key}
        if previous is not None and self._diverged(previous, value, ids, blobs):
            detail["diverged"] = True
        with self._lock:
            self._ensure_current(owner, task)
            self._cache_counts.stored += 1
            record.cache = "stored"
            self._event(owner, "node.cache", detail)

    def _blobs(self, ids: Sequence[str]) -> list[BlobRef]:
        blobs: list[BlobRef] = []
        for artifact_id in ids:
            artifact = self.deps.artifacts.get(artifact_id)
            path = self.deps.artifacts.file(artifact_id)
            if artifact is None or path is None or not artifact.sha256:
                raise ValueError(f"artifact {artifact_id} has no stored file to cache")
            size = artifact.size if artifact.size is not None else path.stat().st_size
            blobs.append(BlobRef(path=path, sha256=artifact.sha256, size=size, name=artifact.name or path.name,
                                 kind=artifact.kind))
        return blobs

    def _diverged(self, previous: CacheEntry, value: JsonValue, ids: Sequence[str], blobs: Sequence[BlobRef]) -> bool:
        old = {blob.artifact: blob.sha256 for blob in previous.blobs if blob.artifact}
        new = {artifact_id: blob.sha256 for artifact_id, blob in zip(ids, blobs, strict=True)}
        return value_digest(_normalized(previous.value, old)) != value_digest(_normalized(value, new))

    def _timeout(self, owner: _Instance) -> float:
        seconds: float | None = None
        model = owner.resolved.model if owner.resolved is not None else None
        if model is not None and "timeout" in type(model).model_fields:
            seconds = _seconds(getattr(model, "timeout", None))
        session = owner.session
        if seconds is None and session is not None:
            seconds = _seconds(session.timeout)
        if seconds is None:
            seconds = self.run_info.options.node_timeout
        if seconds is None:
            seconds = self.config.node_timeout
        if owner.node.op == _EXPECT_VALUE_OP and isinstance(model, ExpectValue) and model.eventually:
            window = _seconds(model.timeout)
            if window is None and session is not None:
                window = _seconds(session.timeout)
            seconds += window if window is not None else DEFAULT_EVENTUALLY_SECONDS
        return seconds

    def _port(self, owner: _Instance) -> DriverPort | None:
        drivers = self.deps.drivers
        if drivers is None:
            return None
        for rid in self._leases(owner):
            handle = self.deps.resources.lease(rid)
            if handle is not None:
                return drivers.port(handle)
        return None

    def _state(self, session: SessionSpec | None) -> SessionState | None:
        if session is None:
            return None
        with self._lock:
            state = self.deps.states.get(session.key)
            if state is None:
                state = SessionState(session.key, persona=session.persona)
                self.deps.states[session.key] = state
            return state

    def _time_context(self, owner: _Instance) -> TimeContext:
        if self._clock_providers is None:
            self._clock_providers = clock_dependencies(self.plan, self.registry)
        providers = self._clock_providers.get(owner.node.id, ())
        return run_time_context(owner.plan, self._clock(owner.plan), providers,
                                lambda node_id: self.deps.values.get(node_id, owner.instance))

    def _clock(self, plan: Plan) -> Clock:
        clock = plan.clock
        supplied = self.run_info.clock
        if supplied is None:
            return clock
        update: dict[str, object] = {"cues": {**clock.cues, **supplied.cues}}
        if supplied.tempo is not None:
            update["tempo"] = supplied.tempo
        return clock.model_copy(update=update)

    def _seed(self, owner: _Instance) -> int:
        root = owner.plan.clock.seed
        if owner.node.hash:
            return node_seed(root, owner.node)
        return derived_seed(root, owner.node.id)

    def _simulated(self, owner: _Instance) -> bool:
        choice = owner.choice
        if choice is not None and choice.fake:
            return True
        port = self._port(owner)
        return port is not None and port.simulated

    def _context(self, owner: _Instance, attempt: int, deadline: float | None) -> ExecContext:
        port = self._port(owner)
        choice = owner.choice
        resolved = self._resolved(owner)
        simulated = (choice is not None and choice.fake) or (port is not None and port.simulated)
        return ExecContext(run=self.run_info, plan=owner.plan, registry=self.registry, node=owner.node,
                           spec=self._spec(owner), instance=owner.instance, attempt=attempt, args=resolved.model,
                           data=resolved.data, session=owner.session, state=self._state(owner.session),
                           resources=self.deps.resources, driver=port, sink=owner.sink_of(),
                           artifacts=owner.store_of(), seed=self._seed(owner), clock=self._clock(owner.plan),
                           deadline=deadline, cancel=self.cancel,
                           reread=lambda expr: self._reread(owner, expr), simulated=simulated,
                           time_context=lambda: self._time_context(owner), content=self.deps.content)

    def _submit(self, owner: _Instance, kind: _TaskKind, prepare: bool = False) -> None:
        pool = self._pool
        if pool is None:
            raise RuntimeError("the scheduler pool is not running")
        leases = self._task_leases(owner, kind)
        held = [rid for rid in leases if rid in self._busy]
        if held:
            raise RuntimeError(f"{owner.node.op} {owner.name} cannot start its {kind} call: lease {held[0]} is held "
                               "by another call")
        if self._load() >= self._capacity():
            raise RuntimeError(f"{owner.node.op} {owner.name} cannot start its {kind} call: {self._load()} calls "
                               f"already run, the limit is {self._capacity()}")
        seconds = self._timeout(owner)
        owner.token += 1
        task = _Task(kind=kind, token=owner.token, deadline=time.monotonic() + seconds, seconds=seconds,
                     leases=leases, prepare=prepare)
        for rid in leases:
            self._busy[rid] = task
        self._inflight[owner] = task
        owner.produced_from = len(owner.store_of().produced)
        attempt = owner.record.attempt
        body = owner.body
        inputs = dict(owner.inputs)
        tree = Path(self.run_info.tree) if self.run_info.tree else None

        def expand() -> _Outcome:
            subplan = self.deps.sandbox.dynamic(owner.plan, owner.node, instance=owner.instance, inputs=inputs,
                                                tree=tree, plugins=self.run_info.options.plugins,
                                                digests=self.deps.digests, session=owner.session)
            return _Outcome(subplan=subplan)

        def execute() -> _Outcome:
            return self._execute(owner, task, attempt, body)

        pool.submit(self._work, owner, task, expand if kind == "dynamic" else execute)

    def _execute(self, owner: _Instance, task: _Task, attempt: int, body: BodyOutcome | None) -> _Outcome:
        choice = owner.choice
        if choice is None:
            raise NodeFailure("executor_missing", f"no executor for {owner.node.op} on this hub")
        executor = choice.executor
        with using_registry(self.registry):
            if task.kind == "enter":
                container = executor.container
                if container is None:
                    raise NodeFailure("executor_error", f"{executor.identity} is not a container executor")
                context = self._context(owner, attempt, task.deadline)
                return _Outcome(entry=container.enter(context), simulated=context.simulated)
            if task.kind == "exit":
                container = executor.container
                if container is None or body is None:
                    raise NodeFailure("executor_error", f"{executor.identity} cannot exit {owner.node.id}")
                context = self._context(owner, attempt, task.deadline)
                return self._produced(owner, task, container.exit(context, body), context.simulated)
            runner = executor.run
            if runner is None:
                raise NodeFailure("executor_error", f"{executor.identity} has no run function for {owner.node.op}")
            if task.prepare:
                hit = self._prepare(owner, task)
                if hit is not None:
                    return hit
            deadline = task.deadline
            slot: SlotHandle | None = None
            if self._spec(owner).location in _RENDER_LOCATIONS:
                slot, deadline = self._hold_slot(owner, task)
            try:
                context = self._context(owner, attempt, deadline)
                result = runner(context)
            finally:
                if slot is not None:
                    self._release_slot(owner, slot)
            return self._produced(owner, task, result, context.simulated)

    def _produced(self, owner: _Instance, task: _Task, result: ExecResult, simulated: bool) -> _Outcome:
        self._check_resolution(owner, result)
        value = checked_output(owner.node, result.value)
        fresh = owner.store_of().produced[owner.produced_from:]
        ids = _dedupe([*fresh, *result.artifacts])
        if not owner.adhoc:
            self._cache_after(owner, task, value, ids, bool(result.soft))
        return _Outcome(result=result, value=value, ids=tuple(ids), simulated=simulated)

    def _hold_slot(self, owner: _Instance, task: _Task) -> tuple[SlotHandle, float]:
        with self._lock:
            task.waiting = True
        try:
            slot = self._claim(owner)
        finally:
            with self._lock:
                task.waiting = False
                task.deadline = time.monotonic() + task.seconds
        return slot, task.deadline

    def _slot_record(self, owner: _Instance, ticket: str, state: Literal["acquiring", "held", "released"],
                     **detail: JsonValue) -> ResourceRecord:
        now = time.time()
        return ResourceRecord(
            run_id=self.run_info.id, resource_id=f"slot:{ticket}", kind="slot",
            session_key=owner.session.key if owner.session is not None else None, state=state, handle=ticket,
            detail={"location": self._spec(owner).location, "node": owner.node.id, "instance": owner.instance,
                    **detail},
            acquired_at=now if state == "held" else None, released_at=now if state == "released" else None)

    def _claim(self, owner: _Instance) -> SlotHandle:
        spec = self._spec(owner)

        def waiting(position: int, ticket: str) -> None:
            self.deps.journal.resource(self._slot_record(owner, ticket, "acquiring", position=position))

        label = f"run {self.run_info.id} {owner.node.op} {owner.node.id}"
        try:
            slot = self.deps.slots.acquire(spec.location, label, run_id=self.run_info.id, cancel=self.cancel,
                                           on_wait=waiting)
        except RuntimeError as error:
            code = "cancelled" if self.cancel.is_set() else "resource_error"
            raise NodeFailure(code, f"no render slot for {owner.node.op} {owner.node.id}: {error}") from error
        self.deps.journal.resource(self._slot_record(owner, slot.ticket, "held", label=label))
        return slot

    def _release_slot(self, owner: _Instance, slot: SlotHandle) -> None:
        try:
            self.deps.slots.release(slot)
        finally:
            self.deps.journal.resource(self._slot_record(owner, slot.ticket, "released"))

    def _work(self, owner: _Instance, task: _Task, body: Callable[[], _Outcome]) -> None:
        outcome: _Outcome | None = None
        error: NodeFailure | None = None
        try:
            outcome = _guard(body)
        except NodeFailure as failure:
            error = failure
        with self._lock:
            self._done.append(_Completion(owner=owner, task=task, outcome=outcome, error=error))
            self._lock.notify_all()

    def _free(self, task: _Task) -> None:
        freed = False
        for rid in task.leases:
            if self._busy.get(rid) is task:
                del self._busy[rid]
                freed = True
        if freed:
            self._lock.notify_all()

    def _apply(self, completion: _Completion) -> None:
        owner = completion.owner
        task = completion.task
        if self._inflight.get(owner) is not task:
            late = self._abandoned.pop(task, None)
            if late is not None:
                self._free(task)
                self._event(late, "node.log", {"level": "warning",
                                               "message": f"the abandoned {task.kind} call of {late.node.op} "
                                                          f"{late.node.id} returned after its timeout; its lease is "
                                                          "free again"})
            return
        del self._inflight[owner]
        self._free(task)
        phase = _TASK_PHASES[task.kind]
        outcome = completion.outcome
        if completion.error is not None or outcome is None:
            failure = completion.error or NodeFailure("executor_error", f"{owner.node.op} returned nothing")
            self._fail(owner, failure, phase)
            return
        if task.kind == "dynamic":
            if outcome.subplan is None:
                self._fail(owner, NodeFailure("sandbox_error", "the sandbox returned no sub-plan"), phase)
                return
            self._expand(owner, outcome.subplan)
            return
        owner.record.simulated = outcome.simulated
        if outcome.hit:
            self._pass(owner, outcome.value, artifacts=list(outcome.ids), detail={"cache": "hit"})
            return
        if task.kind == "enter":
            if outcome.entry is None:
                self._fail(owner, NodeFailure("executor_error", f"{owner.node.op} entered without branches"), phase)
                return
            self._entered(owner, outcome.entry)
            return
        result = outcome.result
        if result is None:
            self._fail(owner, NodeFailure("executor_error", f"{owner.node.op} returned no result"), phase)
            return
        if result.content is not None:
            owner.content = result.content
        if owner.adhoc:
            self._adhoc_done(owner, result, outcome)
            return
        self._pass(owner, outcome.value, artifacts=list(outcome.ids), detail=result.detail, soft=result.soft)

    def _check_resolution(self, owner: _Instance, result: ExecResult) -> None:
        predicted = owner.predicted
        if predicted is None:
            return
        reported = result.resolution
        differing = [name for name in required_resolution(owner.node, self.registry)
                     if _dotted(reported, name) != _dotted(predicted, name)]
        if differing:
            raise NodeFailure("resolution_mismatch",
                              f"{owner.node.op} {owner.node.id} ran with another machine resolution than its "
                              f"executor predicted ({', '.join(differing)})",
                              detail={"fields": cast("list[JsonValue]", differing),
                                      "predicted": cast("JsonValue", predicted),
                                      "reported": cast("JsonValue", reported)})

    def _pass(self, owner: _Instance, value: JsonValue, *, artifacts: Sequence[str] = (),
              detail: Mapping[str, JsonValue] | None = None, soft: Sequence[NodeError] = ()) -> None:
        record = owner.record
        record.output = value
        record.has_output = True
        record.error = soft[0] if soft else None
        record.status = "soft_failed" if soft else "passed"
        record.artifacts = _dedupe([*record.artifacts, *owner.store_of().produced, *artifacts])
        self.deps.values.put(owner.node.id, owner.instance, value)
        shown: dict[str, JsonValue] = dict(detail or {})
        if soft:
            shown["soft"] = [cast("JsonValue", error.model_dump(mode="json")) for error in soft]
        self._finish(owner, "node.soft_failed" if soft else "node.passed", shown)

    def _finish(self, owner: _Instance, event: str, detail: Mapping[str, JsonValue] | None = None) -> None:
        record = owner.record
        record.finished_at = time.time()
        if record.started_at is not None:
            record.duration_ms = max(0, int((record.finished_at - record.started_at) * 1000))
        self._save(owner)
        self._event(owner, event, detail)
        self._settled(owner)

    def _settled(self, owner: _Instance) -> None:
        scope = owner.scope
        if scope is not None and scope.owner is not None and scope.final:
            self._scope_done(scope)

    def _soft_fail(self, owner: _Instance, error: NodeError) -> None:
        record = owner.record
        record.status = "soft_failed"
        record.output = None
        record.has_output = False
        record.error = error
        record.artifacts = _dedupe([*record.artifacts, *owner.store_of().produced])
        self._finish(owner, "node.soft_failed", {"code": error.code, "message": error.message})

    def _stamp(self, owner: _Instance) -> None:
        record = owner.record
        record.artifacts = _dedupe([*record.artifacts, *owner.store_of().produced])
        record.finished_at = time.time()
        if record.started_at is not None:
            record.duration_ms = max(0, int((record.finished_at - record.started_at) * 1000))
        self._save(owner)

    def _cancelled(self, owner: _Instance, why: str) -> None:
        owner.record.status = "cancelled"
        self._stamp(owner)
        self._event(owner, "node.cancelled", {"reason": why})

    def _mark_failed(self, owner: _Instance, error: NodeError) -> None:
        owner.record.status = "failed"
        owner.record.error = error
        self._stamp(owner)
        self._event(owner, "node.failed", {"code": error.code, "message": error.message,
                                           "detail": cast("JsonValue", error.detail)})

    def _retryable(self, owner: _Instance, failure: NodeFailure) -> bool:
        spec = owner.spec
        return (failure.retryable and spec is not None and spec.idempotent and not self._ending
                and owner.retries < self.config.retries)

    def _fail(self, owner: _Instance, failure: NodeFailure, phase: _Phase) -> None:
        if owner.adhoc:
            self._adhoc_failed(owner, failure)
            return
        error = failure.error()
        if failure.soft:
            self._soft_fail(owner, error)
            return
        if failure.code == "cancelled" and self.cancel.is_set():
            owner.record.error = error
            self._cancelled(owner, failure.message)
            return
        if phase == "run" and self._retryable(owner, failure):
            owner.retries += 1
            delay = RETRY_BACKOFF_SECONDS[min(owner.retries - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            owner.retry_at = time.monotonic() + delay
            owner.record.error = error
            self._save(owner)
            self._event(owner, "node.retry", {"attempt": owner.record.attempt, "retry": owner.retries,
                                              "delay": delay, "code": error.code, "message": error.message})
            self._retrying.append(owner)
            return
        if not self._ending:
            decision = self.deps.debugger.at_error(self._probe(owner, error))
            if decision is not None:
                owner.record.error = error
                self._pause_at(owner, "error", phase, decision, error)
                return
        self._fail_hard(owner, error)

    def _fail_hard(self, owner: _Instance, error: NodeError) -> None:
        self._check_abort()
        self._failure_shots(owner)
        self._mark_failed(owner, error)
        if not self._ending:
            self._failure = f"{owner.node.op} {owner.node.id} failed: {error.message}"
            self._begin_ending("failed")
        self._settled(owner)

    def _held_lease(self, owner: _Instance) -> str | None:
        return next((rid for rid in self._leases(owner) if rid in self._busy), None)

    def _failure_shots(self, owner: _Instance) -> None:
        if not self._takes_lease(owner) or not self._leases(owner):
            return
        held = self._held_lease(owner)
        if held is not None:
            self._event(owner, "node.failure_shot", {
                "artifacts": [], "error": f"lease {held} is still held by another call, so no failure shot was taken"})
            return
        try:
            shots = _guard(lambda: self._snapshot(owner, _FAILURE_SNAPSHOTS, "failure"), "driver_error")
        except NodeFailure as failure:
            self._event(owner, "node.failure_shot", {"artifacts": [], "error": failure.message})
            return
        if shots is None:
            return
        self._event(owner, "node.failure_shot", {"artifacts": [shot.id for shot in shots]})

    def _snapshot(self, owner: _Instance, kinds: Sequence[SnapshotKind], tag: str) -> list[Artifact] | None:
        port = self._port(owner)
        if port is None:
            return None
        scratch = self.deps.artifacts.scratch()
        try:
            files = port.snapshot(list(kinds), scratch)
            return [owner.store_of().ingest(file.path, kind=file.kind, name=file.name or file.path.name,
                                            caption=file.caption, tags=[tag]) for file in files]
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _timers(self) -> bool:
        changed = False
        now = time.monotonic()
        for owner, task in list(self._inflight.items()):
            if task.waiting or now < task.deadline + TIMEOUT_GRACE_SECONDS:
                continue
            del self._inflight[owner]
            self._abandoned[task] = owner
            self._fail(owner, NodeFailure(
                "timeout", f"{owner.node.op} {owner.node.id} did not finish within {task.seconds:g} s and was "
                           "abandoned",
                detail={"seconds": task.seconds, "kind": task.kind}), _TASK_PHASES[task.kind])
            changed = True
        if self._ending or self._pause is not None or self._pauses:
            return changed
        for owner in list(self._retrying):
            if owner.retry_at is None or owner.retry_at > now:
                continue
            if self._load() + len(self._queued) >= self._capacity():
                break
            if not self._can_submit(owner, "run"):
                continue
            self._retrying.remove(owner)
            owner.retry_at = None
            record = owner.record
            record.attempt += 1
            record.started_at = time.time()
            record.finished_at = None
            record.duration_ms = None
            self._save(owner)
            self._event(owner, "node.started", {"attempt": record.attempt, "retry": owner.retries})
            self._submit(owner, "run")
            changed = True
        return changed

    def _entered(self, owner: _Instance, entry: ContainerEntry) -> None:
        if self._ending:
            return
        owner.entry = entry
        branches = owner.node.branches or {}
        unknown = [name for name in entry.branches if name not in branches]
        if unknown:
            self._fail(owner, NodeFailure(
                "executor_error", f"{owner.node.op} {owner.node.id} entered unknown branches {', '.join(unknown)}",
                detail={"branches": cast("list[JsonValue]", list(entry.branches))}), "enter")
            return
        if entry.detail:
            self._event(owner, "node.log", {"level": "info", "message": f"{owner.node.op} entered",
                                            "detail": cast("JsonValue", dict(entry.detail))})
        nodes = [node for name in entry.branches for node in branches[name]]
        session = owner.session
        if owner.node.op == TAKE_OP:
            model = self._resolved(owner).model
            session = (model.session if isinstance(model, Take)
                       else SessionSpec.model_validate(owner.node.args["session"]))
        self._activate(owner, nodes, owner.instance, owner.plan, session)

    def _body_values(self, owner: _Instance) -> dict[str, JsonValue]:
        values: dict[str, JsonValue] = {}
        pending: list[Node] = [node for nodes in (owner.node.branches or {}).values() for node in nodes]
        while pending:
            node = pending.pop(0)
            if self.deps.values.has(node.id, owner.instance):
                values[node.id] = self.deps.values.get(node.id, owner.instance)
            for nodes in (node.branches or {}).values():
                pending.extend(nodes)
        return values

    def _scope_done(self, scope: _Scope) -> None:
        owner = scope.owner
        if owner is None or self._ending or owner.final:
            return
        failed = min((member for member in scope.members if member.record.status in ("failed", "cancelled")),
                     key=lambda member: member.record.seq, default=None)
        if failed is not None:
            self._fail_hard(owner, self._body_error(failed))
            return
        op = owner.node.op
        if op == "ctl.each":
            owner.iteration += 1
            if owner.iteration < len(owner.items):
                self._next_iteration(owner)
                return
            self._pass(owner, None, detail={"count": len(owner.items)})
            return
        if op == "ctl.macro":
            model = self._resolved(owner).model
            params = cast("JsonValue", dict(model.params)) if isinstance(model, Macro) else {}
            self._pass(owner, params)
            return
        if op in _NATIVE_CONTAINERS:
            self._pass(owner, None)
            return
        if op == _DYNAMIC_OP:
            subplan = owner.subplan
            child = child_instance(owner.instance, dynamic_segment(owner.node.id))
            try:
                output = resolve(subplan.outputs if subplan is not None else None, self.deps.values, child)
                value = checked_output(owner.node, output)
            except NodeFailure as failure:
                self._fail(owner, failure, "dynamic")
                return
            self._pass(owner, value)
            return
        statuses = {member.record.status for member in scope.members}
        status: NodeStatus = "soft_failed" if "soft_failed" in statuses else "passed"
        entry = owner.entry
        owner.body = BodyOutcome(status=status, branches=entry.branches if entry is not None else (),
                                 values=self._body_values(owner))
        self._queue(owner, "exit")

    def _expand(self, owner: _Instance, sub: SubPlan) -> None:
        if self._ending:
            return
        plan = sub.plan
        issues = self._subplan_issues(owner, plan)
        if issues:
            first = issues[0].get("message")
            more = f" (and {len(issues) - 1} more)" if len(issues) > 1 else ""
            self._fail(owner, NodeFailure(
                "subplan_invalid", f"the sub-plan of ctl.dynamic {owner.node.id} is invalid: {first}{more}",
                detail={"issues": cast("list[JsonValue]", issues)}), "dynamic")
            return
        owner.subplan = plan
        model = self._resolved(owner).model
        fn_hash = model.fn.hash if isinstance(model, Dynamic) else ""
        self.deps.journal.expansion(owner.node.id, owner.instance, "dynamic", {
            "plan": cast("JsonValue", plan.model_dump(mode="json", by_alias=True)),
            "fn": fn_hash,
            "inputs": value_digest(cast("JsonValue", owner.inputs)),
        })
        child = child_instance(owner.instance, dynamic_segment(owner.node.id))
        self._activate(owner, plan.nodes, child, plan, owner.session)

    def _subplan_issues(self, owner: _Instance, plan: Plan) -> list[dict[str, JsonValue]]:
        issues: list[dict[str, JsonValue]] = []
        nodes = [node for node, _ancestors in plan.walk()]
        limit = self.deps.sandbox.limits.max_nodes
        if len(nodes) > limit:
            issues.append({"code": "subplan.nodes",
                           "message": f"the sub-plan has {len(nodes)} nodes, more than the limit of {limit}"})
        parents = {resource.id: resource for resource in owner.plan.resources}
        owned = {resource.id: resource for resource in plan.resources}
        for node in nodes:
            for rid in node.uses:
                theirs = parents.get(rid)
                mine = owned.get(rid)
                if theirs is None:
                    hint = (" A flow holds a backend only when it names seed=, backend= or as_, or calls an op that "
                            "uses one outside eks.dynamic; name the backend on the flow"
                            if self.registry.has_op(node.op) and "backend" in self.registry.op(node.op).resources
                            else "")
                    issues.append({"code": "subplan.resource", "node": node.id,
                                   "message": f"node {node.id} ({node.op}) uses resource {rid}, which the parent "
                                              f"plan does not hold.{hint}"})
                elif mine is None or mine != theirs:
                    issues.append({"code": "subplan.resource", "node": node.id,
                                   "message": f"node {node.id} ({node.op}) uses resource {rid}, whose sub-plan "
                                              "declaration differs from the parent plan's"})
        if self._in_take(owner):
            for node in nodes:
                if node.pin is not None:
                    issues.append({"code": "subplan.pin", "node": node.id,
                                   "message": f"node {node.id} ({node.op}) carries a pin inside a score.take; a "
                                              "dynamic function cannot pin take times"})
        if any(node.op == _DYNAMIC_OP for node in nodes) and self._depth(owner) + 1 > self.config.max_depth:
            issues.append({"code": "subplan.depth",
                           "message": f"the sub-plan nests ctl.dynamic deeper than the limit of "
                                      f"{self.config.max_depth}"})
        if issues:
            return issues
        tree = Path(self.run_info.tree) if self.run_info.tree else None
        report = validate_plan(plan, registry=self.registry, tree=tree)
        for issue in report.errors:
            issues.append(cast("dict[str, JsonValue]", issue.as_dict()))
        return issues

    def _pause_at(self, owner: _Instance, kind: _PauseKind, phase: _Phase, decision: PauseDecision,
                  error: NodeError | None) -> None:
        owner.record.status = "paused"
        self._save(owner)
        self._event(owner, "node.paused", {"reason": decision.reason, "breakpoint": decision.breakpoint,
                                           "kind": kind})
        pause = _Pause(owner=owner, kind=kind, phase=phase, decision=decision, error=error)
        if self._pause is None:
            self._enter_pause(pause)
        else:
            self._pauses.append(pause)

    def _enter_pause(self, pause: _Pause) -> None:
        owner = pause.owner
        snapshots: list[ArtifactRef] = []
        held = self._held_lease(owner) if self._takes_lease(owner) else None
        if held is not None:
            self._event(owner, "node.log", {"level": "warning",
                                            "message": f"pause snapshots skipped: lease {held} is held by another "
                                                       "call"})
        elif self._takes_lease(owner) and self._leases(owner):
            try:
                taken = _guard(lambda: self._snapshot(owner, _PAUSE_SNAPSHOTS, "pause"), "driver_error")
            except NodeFailure as failure:
                taken = None
                self._event(owner, "node.log", {"level": "error",
                                                "message": f"pause snapshots failed: {failure.message}"})
            for artifact in taken or []:
                snapshots.append(ArtifactRef(id=artifact.id, kind=artifact.kind, name=artifact.name, url=artifact.url,
                                             raw_url=artifact.raw_url, node=owner.node.id, instance=owner.instance))
            if snapshots:
                owner.record.artifacts = _dedupe([*owner.record.artifacts, *owner.store_of().produced])
                self._save(owner)
        decision = pause.decision
        record = owner.record
        info = PauseInfo(reason=decision.reason, node=owner.node.id, instance=owner.instance, attempt=record.attempt,
                         op=owner.node.op, label=decision.label or owner.node.label, src=owner.node.src,
                         args=record.args, error=pause.error, breakpoint=decision.breakpoint,
                         values=self.deps.values.snapshot(), snapshots=snapshots, at=time.time())
        pause.info = info
        pause.started = time.monotonic()
        self._pause = pause
        self._status = "paused"
        self.deps.debugger.enter_pause(info)
        self.deps.journal.status("paused", reason=f"paused at {owner.node.op} {owner.node.id} ({decision.reason})",
                                 pause=info)
        self._publish(force=True)

    def _resume(self, mode: Literal["continue", "step"]) -> None:
        self._pause = None
        self._status = "running"
        self.deps.debugger.leave_pause(mode)
        self.deps.journal.status("running")
        self._publish(force=True)

    def _answer(self, ticket: ControlTicket, ok: bool, message: str, result: JsonValue = None) -> None:
        self.deps.debugger.answer(ticket, ControlResponse(ok=ok, status=self._status,
                                                          debugger=self.deps.debugger.state, message=message,
                                                          result=result))

    def _serve(self, ticket: ControlTicket) -> None:
        request = ticket.request

        def answer(ok: bool, message: str) -> None:
            self._answer(ticket, ok, message)

        if request.action == "abort":
            self._set_abort("aborted from the debugger")
            self.cancel.set()
            answer(True, "the run is aborting")
            return
        pause = self._pause
        if pause is None:
            answer(False, f"the run is not paused; {request.action} needs a paused run")
            return
        owner = pause.owner
        if request.node is not None and request.node != owner.node.id:
            answer(False, f"the run is paused at {owner.node.id}, not at {request.node}")
            return
        action = request.action
        if action in ("continue", "step"):
            self._resume("step" if action == "step" else "continue")
            self._carry_on(pause)
            answer(True, f"{action} from {owner.node.op} {owner.node.id}")
        elif action == "retry":
            self._resume("continue")
            self._retry(pause)
            answer(True, f"retrying {owner.node.op} {owner.node.id}")
        elif action == "skip":
            value = request.value
            problem = output_problem(owner.node.out, value) if value is not None else None
            if problem is not None:
                answer(False, f"cannot skip {owner.node.id} with that value: {problem}")
                return
            self._resume("continue")
            self._skip(owner, value)
            answer(True, f"skipped {owner.node.op} {owner.node.id}")
        elif action == "edit":
            ok, message = self._edit(owner, request)
            answer(ok, message)
        elif action == "do":
            pause.started = time.monotonic()
            refused = self._adhoc(owner, ticket)
            if refused is not None:
                answer(False, refused)
        else:
            answer(False, f"{action} is not an action for a paused run")

    def _carry_on(self, pause: _Pause) -> None:
        owner = pause.owner
        if pause.kind == "error":
            self._fail_hard(owner, pause.error or NodeError(code="executor_error", message="failed"))
            return
        owner.record.status = "running"
        self._save(owner)
        if pause.kind == "source":
            self._pass(owner, None, detail={"resumed": True})
            return
        self._proceed(owner)

    def _retry(self, pause: _Pause) -> None:
        owner = pause.owner
        if pause.kind != "error":
            self._carry_on(pause)
            return
        record = owner.record
        record.status = "running"
        record.attempt += 1
        record.error = None
        record.started_at = time.time()
        record.finished_at = None
        record.duration_ms = None
        self._save(owner)
        self._event(owner, "node.started", {"attempt": record.attempt, "retry": "debugger"})
        if pause.phase == "exit":
            self._queue(owner, "exit")
            return
        self._proceed(owner)

    def _skip(self, owner: _Instance, value: JsonValue) -> None:
        record = owner.record
        record.status = "skipped"
        record.output = value
        record.has_output = True
        record.error = None
        self.deps.values.put(owner.node.id, owner.instance, value)
        if not owner.bodies:
            for nodes in (owner.node.branches or {}).values():
                self._skip_nodes(nodes, owner.instance, owner, "the container was skipped in the debugger")
        self._finish(owner, "node.skipped", {"by": "debugger"})

    def _edit(self, owner: _Instance, request: ControlRequest) -> tuple[bool, str]:
        if owner.spec is None:
            return False, f"op {owner.node.op} is not registered on this hub"
        try:
            with using_registry(self.registry):
                resolved = validate_args(owner.spec, cast("JsonValue", dict(request.args or {})))
        except NodeFailure as failure:
            return False, failure.message
        owner.resolved = resolved
        owner.edited = True
        owner.record.args = resolved.data
        owner.record.edited = True
        self._save(owner)
        self._event(owner, "node.args_edited", {"args": cast("JsonValue", resolved.data)})
        return True, f"edited the arguments of {owner.node.op} {owner.node.id}"

    def _role_resources(self, spec: OpSpec, session: SessionSpec | None, plan: Plan) -> list[str]:
        chosen: list[str] = []
        for role in spec.resources:
            for resource in plan.resources:
                matches = resource.kind.startswith(_LEASE_PREFIX) if role == "lease" else resource.kind == role
                same = session is None or resource.session in (session.key, None)
                if matches and same and resource.id not in chosen:
                    chosen.append(resource.id)
                    break
        return chosen

    def _adhoc(self, paused: _Instance, ticket: ControlTicket) -> str | None:
        request = ticket.request
        name = request.op
        if not name:
            return "do needs an op"
        if not self.registry.has_op(name):
            return f"there is no op {name} in this run's registry"
        spec = self.registry.op(name)
        if spec.container or name in NATIVE_OPS:
            return f"{name} cannot run as an ad-hoc op; only plain ops can"
        session = paused.session if paused.session is not None else self.plan.session
        try:
            with using_registry(self.registry):
                resolved = validate_args(spec, cast("JsonValue", dict(request.args or {})))
        except NodeFailure as failure:
            return failure.message
        choice = self._sub_choice(spec)
        if choice is None or choice.native:
            return f"no executor for {name} on this hub"
        if choice.executor.run is None:
            return f"{choice.executor.identity} cannot run {name}"
        self._adhoc_count += 1
        node = Node.model_construct(id=f"{_ADHOC_PREFIX}{self._adhoc_count}", op=name, v=spec.version,
                                    args=dict(resolved.data), uses=self._role_resources(spec, session, paused.plan),
                                    after=[], out=spec.out, pin=None, label=None, src=None, branches=None, hash="")
        owner = self._create(node, ROOT_INSTANCE, paused.plan, session, None, None)
        owner.adhoc = True
        owner.choice = choice
        owner.resolved = resolved
        record = owner.record
        record.executor = choice.executor.identity
        record.status = "ready"
        record.queued_at = time.time()
        self._event(owner, "node.ready", {"adhoc": True})
        record.status = "running"
        record.attempt = 1
        record.args = resolved.data
        record.started_at = time.time()
        self._save(owner)
        self._event(owner, "node.started", {"attempt": 1, "adhoc": True})
        self._tickets[owner] = ticket
        self._queue(owner, "run", True)
        return None

    def _adhoc_done(self, owner: _Instance, result: ExecResult, outcome: _Outcome) -> None:
        record = owner.record
        soft = list(result.soft)
        record.output = outcome.value
        record.has_output = True
        record.error = soft[0] if soft else None
        record.status = "soft_failed" if soft else "passed"
        record.artifacts = _dedupe([*record.artifacts, *owner.store_of().produced, *outcome.ids])
        self._stamp(owner)
        detail: dict[str, JsonValue] = {**dict(result.detail), "adhoc": True}
        if soft:
            detail["soft"] = [cast("JsonValue", error.model_dump(mode="json")) for error in soft]
        self._event(owner, "node.soft_failed" if soft else "node.passed", detail)
        verb = "soft-failed" if soft else "passed"
        self._answer_adhoc(owner, True, f"{owner.node.op} {verb} as {owner.node.id}", outcome.value)

    def _adhoc_failed(self, owner: _Instance, failure: NodeFailure) -> None:
        error = failure.error()
        if failure.code == "cancelled" and self.cancel.is_set():
            owner.record.error = error
            self._cancelled(owner, failure.message)
        elif failure.soft:
            self._soft_fail(owner, error)
        else:
            self._mark_failed(owner, error)
        self._answer_adhoc(owner, False, f"{owner.node.op} failed: {failure.message}")

    def _answer_adhoc(self, owner: _Instance, ok: bool, message: str, result: JsonValue = None) -> None:
        if self._pause is not None:
            self._pause.started = time.monotonic()
        ticket = self._tickets.pop(owner, None)
        if ticket is not None:
            self._answer(ticket, ok, message, result)

    def _hold_leases(self, consumer: _Instance, producer: _Instance) -> _Task:
        current = self._inflight.get(consumer)
        if current is None:
            raise NodeFailure("cancelled", f"{consumer.node.op} {consumer.node.id} is no longer running")
        needed = tuple(rid for rid in self._leases(producer) if self._busy.get(rid) is not current)
        while True:
            if self.cancel.is_set():
                raise NodeFailure("cancelled", f"the run was cancelled before {consumer.node.op} {consumer.node.id} "
                                               f"could re-read {producer.node.op} {producer.node.id}")
            if self._inflight.get(consumer) is not current:
                raise NodeFailure("cancelled", f"{consumer.node.op} {consumer.node.id} was abandoned while it waited "
                                               f"to re-read {producer.node.op} {producer.node.id}")
            busy = next((rid for rid in needed if rid in self._busy), None)
            if busy is None:
                break
            remaining = current.deadline - time.monotonic()
            if remaining <= 0:
                raise NodeFailure("timeout", f"{consumer.node.op} {consumer.node.id} could not re-read "
                                             f"{producer.node.op} {producer.node.id}: lease {busy} stayed busy until "
                                             "its deadline", detail={"lease": busy})
            self._lock.wait(min(remaining, WAIT_SLICE_SECONDS))
        hold = _Task(kind="reread", token=0, deadline=current.deadline, seconds=current.seconds, leases=needed)
        for rid in needed:
            self._busy[rid] = hold
        return hold

    def _reread(self, consumer: _Instance, expr: JsonValue) -> JsonValue:
        target = expr.get(REF_KEY) if isinstance(expr, dict) else None
        if not isinstance(target, str):
            raise NodeFailure("assertion", f"{consumer.node.op} {consumer.node.id} can re-read only a $ref, not a "
                                           f"{_json_type(expr)}")
        with self._lock:
            producer = self._visible(target, consumer.instance)
            if producer is None:
                raise NodeFailure("assertion", f"node {target} has no instance visible from {consumer.name}")
            spec = producer.spec
            choice = producer.choice
            if spec is None or not spec.idempotent or choice is None or choice.native:
                raise NodeFailure("assertion", f"{producer.node.op} {target} is not idempotent, so "
                                               f"{consumer.node.op} {consumer.node.id} cannot re-read it; only "
                                               "idempotent producers are re-executed")
            if producer.resolved is None:
                raise NodeFailure("assertion", f"{producer.node.op} {target} has not run yet")
            runner = choice.executor.run
            if runner is None:
                raise NodeFailure("assertion", f"{choice.executor.identity} cannot re-run {producer.node.op}")
            hold = self._hold_leases(consumer, producer)
            producer.record.attempt += 1
            attempt = producer.record.attempt
            self._save(producer)
            self._event(producer, "node.reread", {"consumer": consumer.node.id, "instance": consumer.instance,
                                                  "attempt": attempt})
        try:
            deadline = time.monotonic() + self._timeout(producer)
            slot = self._claim(producer) if spec.location in _RENDER_LOCATIONS else None
            try:
                def call() -> ExecResult:
                    with using_registry(self.registry):
                        return runner(self._context(producer, attempt, deadline))

                result = _guard(call)
            finally:
                if slot is not None:
                    self._release_slot(producer, slot)
            value = checked_output(producer.node, result.value)
        finally:
            with self._lock:
                self._free(hold)
        with self._lock:
            self.deps.values.put(producer.node.id, producer.instance, value)
            producer.record.output = value
            producer.record.has_output = True
            producer.record.artifacts = _dedupe([*producer.record.artifacts, *producer.store_of().produced,
                                                 *result.artifacts])
            self._save(producer)
        return resolve(expr, self.deps.values, consumer.instance)


__all__ = [
    "DATA_CREATE_OPS",
    "DATA_EXTEND_OP",
    "RETRY_BACKOFF_SECONDS",
    "TIMEOUT_GRACE_SECONDS",
    "WAIT_SLICE_SECONDS",
    "RunOutcome",
    "Scheduler",
    "SchedulerConfig",
    "SchedulerDeps",
    "SchedulerStatus",
]
