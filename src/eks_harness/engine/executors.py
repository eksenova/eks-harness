from __future__ import annotations

import importlib
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from eks_harness.api.links import Links
from eks_harness.config import Config
from eks_harness.daemon.events import EventBus
from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.engine.drivers import DriverPort
from eks_harness.engine.errors import ContentError, NodeFailure
from eks_harness.engine.model import (
    BackendHandle,
    FakeProfile,
    LeaseHandle,
    NodeError,
    NodeStatus,
    RunInfo,
)
from eks_harness.ir.plan import Clock, Node, Plan, SessionSpec
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    Location,
    OpSpec,
    Provenance,
    Registry,
    builtin_version,
)
from eks_harness.ir.time import TimeContext
from eks_harness.ir.types import Artifact, MediaInfo
from eks_harness.store import layout
from eks_harness.store.artifacts import ingest_file

NATIVE_OPS: frozenset[str] = frozenset({
    "ctl.when", "ctl.each", "ctl.parallel", "ctl.macro", "ctl.dynamic", "debug.break", "debug.pause_on_error",
})
NATIVE_EXECUTOR = "eks.native"
NATIVE_VERSION = "1"
PLUGIN_EXECUTOR_PREFIX = "plugin:"
MATCH_EXACT = 3
MATCH_FAMILY = 2
MATCH_ANY = 1
NO_MATCH = 0
_PROGRESS_INTERVAL = 0.25
_SCRATCH_FOLDER = "engine"
_JSON: TypeAdapter[Any] = TypeAdapter(cast(Any, JsonValue))


def builtin_provenance() -> Provenance:
    return Provenance(BUILTIN_PLUGIN, builtin_version())


@dataclass(frozen=True, slots=True)
class BlobRef:
    path: Path
    sha256: str
    size: int
    name: str
    kind: str | None = None


@dataclass
class ExecResult:
    value: JsonValue = None
    content: str | None = None
    artifacts: list[str] = field(default_factory=list[str])
    resolution: dict[str, JsonValue] | None = None
    soft: list[NodeError] = field(default_factory=list[NodeError])
    detail: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class ContainerEntry:
    branches: tuple[str, ...] = ("body",)
    detail: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class BodyOutcome:
    status: NodeStatus
    branches: tuple[str, ...]
    values: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])


class NodeSink(Protocol):
    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None: ...

    def progress(self, fraction: float, message: str) -> None: ...

    def artifact(self, artifact: Artifact) -> None: ...


class ArtifactStore(Protocol):
    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact: ...

    def get(self, artifact_id: str) -> Artifact | None: ...

    def file(self, artifact_id: str) -> Path | None: ...

    def scratch(self) -> Path: ...


class ResourceView(Protocol):
    def lease(self, resource_id: str) -> LeaseHandle | None: ...

    def backend(self, resource_id: str) -> BackendHandle | None: ...


class DaemonArtifacts:
    def __init__(self, db: Database, config: Config, events: EventBus | None = None) -> None:
        self.db = db
        self.config = config
        self.events = events

        self.links = Links(config)

    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact:
        row = ingest_file(self.db, self.config, project=project, session=session, path_or_stream=path, kind=kind,
                          caption=caption, tags=list(tags), meta=dict(meta), source="agent", user=actor,
                          filename=name, events=self.events, move=move)
        stored = artifacts_repo.get(self.db.conn(), row.id)
        return self._artifact(stored if stored is not None else row)

    def get(self, artifact_id: str) -> Artifact | None:
        row = artifacts_repo.get(self.db.conn(), artifact_id)
        return self._artifact(row) if row is not None else None

    def file(self, artifact_id: str) -> Path | None:
        row = artifacts_repo.get(self.db.conn(), artifact_id)
        if row is None:
            return None
        path = layout.file_path(self.config.paths, row.rel_path)
        return path if path.is_file() else None

    def meta(self, artifact_id: str) -> Mapping[str, JsonValue]:
        row = artifacts_repo.get(self.db.conn(), artifact_id)
        return dict(row.meta or {}) if row is not None else {}

    def scratch(self) -> Path:
        base = self.config.paths.tmp_dir / _SCRATCH_FOLDER
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="scratch-", dir=base))

    def _artifact(self, row: artifacts_repo.Artifact) -> Artifact:
        return Artifact(
            id=row.id,
            kind=row.kind,
            name=row.filename,
            url=self.links.artifact(row.project_id, row.session_slug, row.id),
            raw_url=self.links.raw(row.id, row.filename),
            session_url=self.links.session(row.project_id, row.session_slug),
            mime=row.mime,
            size=row.size,
            sha256=f"sha256:{row.sha256}",
        )


class ArtifactSink:
    def __init__(self, store: ArtifactStore, *, run: RunInfo, node_id: str, instance: str, op: str,
                 sink: NodeSink) -> None:
        self.store = store
        self.run = run
        self.node_id = node_id
        self.instance = instance
        self.op = op
        self.sink = sink
        self.produced: list[str] = []

    def ingest(self, path: Path, *, kind: str | None = None, name: str | None = None, caption: str = "",
               tags: Sequence[str] = (), meta: Mapping[str, JsonValue] | None = None, move: bool = True) -> Artifact:
        all_tags = list(dict.fromkeys([*tags, f"run:{self.run.id}", f"node:{self.node_id}", self.op]))
        all_meta: dict[str, JsonValue] = {
            **dict(meta or {}),
            "run": self.run.id,
            "node": self.node_id,
            "instance": self.instance,
            "op": self.op,
        }
        artifact = self.store.ingest(path, project=self.run.project, session=self.run.session, kind=kind, name=name,
                                     caption=caption, tags=all_tags, meta=all_meta, actor=self.run.actor, move=move)
        self.produced.append(artifact.id)
        self.sink.artifact(artifact)
        return artifact

    def write(self, data: bytes, *, filename: str, kind: str | None = None, caption: str = "",
              tags: Sequence[str] = (), meta: Mapping[str, JsonValue] | None = None) -> Artifact:
        name = Path(filename).name
        if not name or name in {".", ".."}:
            raise ValueError(f"artifact file name {filename!r} names no file")
        directory = self.store.scratch()
        try:
            path = directory / name
            path.write_bytes(data)
            return self.ingest(path, kind=kind, name=name, caption=caption, tags=tags, meta=meta, move=True)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def get(self, artifact_id: str) -> Artifact | None:
        return self.store.get(artifact_id)

    def file(self, artifact_id: str) -> Path | None:
        return self.store.file(artifact_id)


@dataclass
class SessionState:
    key: str
    persona: str | None = None
    values: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])


Reread = Callable[[JsonValue], JsonValue]


@dataclass(frozen=True, slots=True)
class MediaFile:
    path: Path
    info: MediaInfo
    sha256: str | None = None


class MediaContentLike(Protocol):
    @property
    def path(self) -> Path | None: ...

    @property
    def sha256(self) -> str: ...


class MediaResolver(Protocol):
    def media(self, uri: str) -> MediaContentLike: ...


class ExecContext:
    def __init__(self, *, run: RunInfo, plan: Plan, registry: Registry, node: Node, spec: OpSpec, instance: str,
                 attempt: int, args: BaseModel, data: dict[str, JsonValue], session: SessionSpec | None,
                 state: SessionState | None, resources: ResourceView, driver: DriverPort | None, sink: NodeSink,
                 artifacts: ArtifactSink, seed: int, clock: Clock, deadline: float | None, cancel: threading.Event,
                 reread: Reread | None = None, simulated: bool = False,
                 time_context: Callable[[], TimeContext] | None = None,
                 content: MediaResolver | None = None) -> None:
        self.run = run
        self.plan = plan
        self.registry = registry
        self.node = node
        self.spec = spec
        self.instance = instance
        self.attempt = attempt
        self.args = args
        self.data = data
        self.session = session
        self.state = state
        self.resources = resources
        self.driver = driver
        self.sink = sink
        self.artifacts = artifacts
        self.seed = seed
        self.clock = clock
        self.deadline = deadline
        self.cancel = cancel
        self.simulated = simulated
        self._reread = reread
        self._time_context = time_context
        self._time: TimeContext | None = None
        self._content = content
        self._progress_lock = threading.Lock()
        self._progress_at: float | None = None

    @property
    def op(self) -> str:
        return self.spec.name

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise NodeFailure("cancelled", f"the run was cancelled while node {self.node.id} ({self.op}) ran")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise NodeFailure("timeout", f"node {self.node.id} ({self.op}) passed its deadline")

    def log(self, message: str, *, level: str = "info", detail: Mapping[str, JsonValue] | None = None) -> None:
        self.sink.event("node.log", {"level": level, "message": message, **dict(detail or {})})

    def event(self, type: str, detail: Mapping[str, JsonValue] | None = None) -> None:
        self.sink.event(type if "." in type else f"node.{type}", dict(detail or {}))

    def progress(self, fraction: float, message: str = "") -> None:
        now = time.monotonic()
        with self._progress_lock:
            last = self._progress_at
            if fraction < 1.0 and last is not None and now - last < _PROGRESS_INTERVAL:
                return
            self._progress_at = now
        self.sink.progress(fraction, message)

    def lease(self) -> LeaseHandle:
        resource_id = self._resource_id("lease")
        handle = self.resources.lease(resource_id)
        if handle is None:
            raise NodeFailure("resource_error",
                              f"lease {resource_id} of node {self.node.id} ({self.op}) is not held by the run",
                              detail={"resource": resource_id})
        return handle

    def backend(self) -> BackendHandle:
        resource_id = self._resource_id("backend")
        handle = self.resources.backend(resource_id)
        if handle is None:
            raise NodeFailure("resource_error",
                              f"backend {resource_id} of node {self.node.id} ({self.op}) is not held by the run",
                              detail={"resource": resource_id})
        return handle

    def reread(self, expr: JsonValue) -> JsonValue:
        if self._reread is None:
            raise NodeFailure("assertion",
                              f"node {self.node.id} ({self.op}) cannot re-read a value: re-reads need the scheduler, "
                              "and this execution context has none")
        return self._reread(expr)

    def time(self) -> TimeContext:
        if self._time is None:
            if self._time_context is None:
                raise NodeFailure("time_unresolved",
                                  f"node {self.node.id} ({self.op}) has no clock: this execution context has no "
                                  "scheduler")
            self._time = self._time_context()
        return self._time

    def media(self, value: JsonValue) -> MediaFile:
        if not isinstance(value, Mapping):
            raise NodeFailure("content_unavailable", f"node {self.node.id} ({self.op}): {value!r} is not a media value")
        info = MediaInfo.model_validate(value)
        artifact = info.artifact
        if artifact is not None:
            path = self.artifacts.file(artifact)
            if path is None:
                raise NodeFailure("content_unavailable", f"artifact {artifact} of {info.uri} has no stored file",
                                  detail={"uri": info.uri})
            stored = self.artifacts.get(artifact)
            return MediaFile(path=path, info=info, sha256=stored.sha256 if stored is not None else None)
        if self._content is None:
            raise NodeFailure("content_unavailable",
                              f"node {self.node.id} ({self.op}) cannot read {info.uri}: no content resolver")
        try:
            found = self._content.media(info.uri)
        except ContentError as error:
            raise NodeFailure(error.code, error.message, detail=dict(error.detail)) from error
        if found.path is None:
            raise NodeFailure("content_unavailable", f"{info.uri} has no local file", detail={"uri": info.uri})
        return MediaFile(path=found.path, info=info, sha256=found.sha256)

    def _resource_id(self, role: str) -> str:
        kinds = {resource.id: resource.kind for resource in self.plan.resources}
        for resource_id in self.node.uses:
            kind = kinds.get(resource_id)
            if kind is None:
                continue
            if (role == "lease" and kind.startswith("lease.")) or kind == role:
                return resource_id
        raise NodeFailure("resource_error",
                          f"node {self.node.id} ({self.op}) uses no {role} resource of the plan",
                          detail={"uses": list(self.node.uses)})


@dataclass(frozen=True, slots=True)
class ResolveContext:
    run: RunInfo
    plan: Plan
    registry: Registry
    node: Node
    spec: OpSpec
    args: BaseModel
    data: Mapping[str, JsonValue]
    resources: ResourceView


Run = Callable[[ExecContext], ExecResult]
Resolver = Callable[[ResolveContext], Mapping[str, JsonValue]]


class ContainerExecutor(Protocol):
    def enter(self, call: ExecContext, /) -> ContainerEntry: ...

    def exit(self, call: ExecContext, body: BodyOutcome, /) -> ExecResult: ...


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    version: str
    ops: tuple[str, ...]
    run: Run | None = None
    container: ContainerExecutor | None = None
    resolver: Resolver | None = None
    locations: frozenset[Location] | None = None
    runtime_version: Callable[[], str] | None = None
    fonts: bool = False
    fake: bool = False
    provenance: Provenance = field(default_factory=builtin_provenance)
    description: str = ""

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def match(self, op: OpSpec) -> int:
        if self.locations is not None and op.location not in self.locations:
            return NO_MATCH
        if op.container and self.container is None:
            return NO_MATCH
        if not op.container and self.run is None:
            return NO_MATCH
        return max((pattern_matches(pattern, op) for pattern in self.ops), default=NO_MATCH)


@dataclass(frozen=True, slots=True)
class ExecutorChoice:
    executor: ExecutorSpec
    fake: bool = False
    native: bool = False
    reason: str = ""


class FakeSource(Protocol):
    def executor(self, op: OpSpec) -> ExecutorSpec | None: ...


def pattern_matches(pattern: str, op: OpSpec) -> int:
    if pattern == op.name:
        return MATCH_EXACT
    if pattern in (op.family, f"{op.family}.*"):
        return MATCH_FAMILY
    if pattern == "*":
        return MATCH_ANY
    return NO_MATCH


def _native_run(call: ExecContext) -> ExecResult:
    raise NodeFailure("executor_error", f"native ops run in the scheduler, not as executors ({call.op})")


def native_executor() -> ExecutorSpec:
    return ExecutorSpec(NATIVE_EXECUTOR, NATIVE_VERSION, ops=tuple(sorted(NATIVE_OPS)), run=_native_run,
                        description="Control flow and debug ops the scheduler runs itself")


def as_result(value: object) -> ExecResult:
    if isinstance(value, ExecResult):
        return value
    if value is None:
        return ExecResult()
    if isinstance(value, BaseModel):
        return ExecResult(value=value.model_dump(mode="json", by_alias=True))
    try:
        checked = _JSON.validate_python(value, strict=True)
    except ValidationError as problem:
        raise NodeFailure("executor_error",
                          f"the executor returned a {type(value).__name__}, which is not a JSON value, an ExecResult "
                          "or a pydantic model",
                          detail={"type": type(value).__name__, "error": str(problem.errors()[0]["msg"])}) from None
    return ExecResult(value=checked)


class _PluginContainer:
    def __init__(self, enter: Callable[[ExecContext], object], exit: Callable[[ExecContext, BodyOutcome], object],
                 reference: str) -> None:
        self._enter = enter
        self._exit = exit
        self.reference = reference

    def enter(self, call: ExecContext, /) -> ContainerEntry:
        entered = self._enter(call)
        if entered is None:
            return ContainerEntry()
        if isinstance(entered, ContainerEntry):
            return entered
        if isinstance(entered, (list, tuple)) and all(isinstance(item, str) for item in cast(Sequence[object], entered)):
            return ContainerEntry(branches=tuple(cast(Sequence[str], entered)))
        raise NodeFailure("executor_error",
                          f"container executor {self.reference} entered with a {type(entered).__name__}; return a "
                          "ContainerEntry, a list of branch names or None")

    def exit(self, call: ExecContext, body: BodyOutcome, /) -> ExecResult:
        return as_result(self._exit(call, body))


def _plugin_run(target: Callable[[ExecContext], object]) -> Run:
    def run(call: ExecContext) -> ExecResult:
        return as_result(target(call))

    return run


def _reference_error(reference: str, cause: str) -> ValueError:
    return ValueError(f"executor reference {reference!r}: {cause}")


def _resolve_reference(reference: str) -> object:
    module_name, colon, qualname = reference.partition(":")
    if not colon or not module_name or not qualname:
        raise _reference_error(reference, "it is not 'module:qualname'")
    try:
        found: object = importlib.import_module(module_name)
    except Exception as problem:
        raise _reference_error(reference, f"module {module_name} cannot be imported: {type(problem).__name__}: "
                                          f"{problem}") from problem
    for part in qualname.split("."):
        try:
            found = getattr(found, part)
        except AttributeError:
            owner = getattr(found, "__name__", module_name)
            raise _reference_error(reference, f"{part!r} is not defined in {owner}") from None
    if isinstance(found, type):
        try:
            found = found()
        except Exception as problem:
            raise _reference_error(reference, f"the class cannot be instantiated without arguments: "
                                              f"{type(problem).__name__}: {problem}") from problem
    return found


class ExecutorRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._specs: list[ExecutorSpec] = []

    def register(self, spec: ExecutorSpec) -> ExecutorSpec:
        if not spec.name:
            raise ValueError("an executor needs a name")
        if not spec.version:
            raise ValueError(f"executor {spec.name} needs a version")
        if (spec.run is None) == (spec.container is None):
            raise ValueError(f"executor {spec.identity} needs exactly one of run (plain ops) and container "
                             "(container ops)")
        with self._lock:
            for existing in self._specs:
                overlap = sorted(set(existing.ops) & set(spec.ops))
                if existing.name == spec.name and overlap:
                    raise ValueError(f"executor {spec.name} is already registered for {', '.join(overlap)}")
            self._specs.append(spec)
        return spec

    def specs(self) -> list[ExecutorSpec]:
        with self._lock:
            return list(self._specs)

    def real(self, op: OpSpec) -> ExecutorSpec | None:
        best: ExecutorSpec | None = None
        best_score = NO_MATCH
        for spec in self.specs():
            score = spec.match(op)
            if score != NO_MATCH and score >= best_score:
                best, best_score = spec, score
        if best is not None and (best_score == MATCH_EXACT or op.executor is None):
            return best
        return self.plugin_executor(op)

    def plugin_executor(self, op: OpSpec) -> ExecutorSpec | None:
        reference = op.executor
        if reference is None:
            return None
        target = _resolve_reference(reference)
        name = f"{PLUGIN_EXECUTOR_PREFIX}{op.provenance.plugin}:{op.name}"
        version = op.executor_version or op.provenance.version or "0"
        description = f"Plugin executor {reference} of {op.name}"
        if op.container:
            enter: object = getattr(target, "enter", None)
            exit_: object = getattr(target, "exit", None)
            missing = [part for part, found in (("enter", enter), ("exit", exit_)) if not callable(found)]
            if missing:
                raise _reference_error(reference, f"container op {op.name} needs an object with enter and exit, and "
                                                  f"{type(target).__name__} has no {' or '.join(missing)}")
            container = _PluginContainer(cast(Callable[[ExecContext], object], enter),
                                         cast(Callable[[ExecContext, BodyOutcome], object], exit_), reference)
            return ExecutorSpec(name, version, ops=(op.name,), container=container, provenance=op.provenance,
                                description=description)
        if not callable(target):
            raise _reference_error(reference, f"{type(target).__name__} is not callable with an ExecContext")
        return ExecutorSpec(name, version, ops=(op.name,),
                            run=_plugin_run(cast(Callable[[ExecContext], object], target)),
                            provenance=op.provenance, description=description)

    def choose(self, op: OpSpec, *, fakes: FakeProfile | None = None,
               fake_source: FakeSource | None = None) -> ExecutorChoice | None:
        if op.name in NATIVE_OPS:
            return ExecutorChoice(native_executor(), native=True, reason=f"{op.name} is a native op of the scheduler")
        if fakes is not None and fake_source is not None:
            forced = _fake_choice(op, fakes.force, fake_source, "the fake profile forces")
            if forced is not None:
                return forced
        real = self.real(op)
        if real is not None:
            return ExecutorChoice(real, reason=f"real executor {real.identity}")
        if fakes is not None and fake_source is not None:
            return _fake_choice(op, fakes.families, fake_source, "no real executor; the fake profile covers")
        return None


def _fake_choice(op: OpSpec, patterns: Sequence[str], source: FakeSource, why: str) -> ExecutorChoice | None:
    pattern = _covering(patterns, op)
    if pattern is None:
        return None
    spec = source.executor(op)
    if spec is None:
        return None
    return ExecutorChoice(spec, fake=True, reason=f"{why} {pattern!r}")

def _covering(patterns: Sequence[str], op: OpSpec) -> str | None:
    return next((pattern for pattern in patterns if pattern_matches(pattern, op) != NO_MATCH), None)


__all__ = [
    "MATCH_ANY",
    "MATCH_EXACT",
    "MATCH_FAMILY",
    "NATIVE_EXECUTOR",
    "NATIVE_OPS",
    "NATIVE_VERSION",
    "NO_MATCH",
    "PLUGIN_EXECUTOR_PREFIX",
    "ArtifactSink",
    "ArtifactStore",
    "BlobRef",
    "BodyOutcome",
    "ContainerEntry",
    "ContainerExecutor",
    "DaemonArtifacts",
    "ExecContext",
    "ExecResult",
    "ExecutorChoice",
    "ExecutorRegistry",
    "ExecutorSpec",
    "FakeSource",
    "NodeSink",
    "Reread",
    "ResolveContext",
    "Resolver",
    "ResourceView",
    "Run",
    "SessionState",
    "as_result",
    "builtin_provenance",
    "native_executor",
    "pattern_matches",
]
