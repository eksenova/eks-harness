from __future__ import annotations

import importlib
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, get_args

from pydantic import Field, JsonValue

from eks_harness import renderq
from eks_harness.api.errors import ApiError
from eks_harness.engine.errors import ResourceError
from eks_harness.engine.model import (
    MAIN_SESSION,
    BackendHandle,
    BackendIdentity,
    BackendRef,
    BackendSource,
    EngineModel,
    LeaseHandle,
    ResourceKind,
    ResourceRecord,
    RunInfo,
    RunResources,
    SeedMode,
    SeedRestoreResult,
    SlotHandle,
)
from eks_harness.ir.hashing import canonical_json, digest
from eks_harness.ir.plan import Node, Plan, Resource, SessionSpec
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    PROJECT_PLUGIN,
    Location,
    Registry,
    RegistryError,
)
from eks_harness.plugins.sdk import BackendChoice

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext
    from eks_harness.daemon.leases import LeaseManager

LEASE_KIND_BY_RESOURCE: Mapping[str, str] = {"lease.web": "browser", "lease.ios": "ios", "lease.android": "android"}
SLOT_LOCATIONS: frozenset[str] = frozenset({"node", "blender", "browser_scene"})
CASSETTE_OP = "net.cassette"
SEED_PLAN_KIND = "seed"
RUN_INSTANCE_PREFIX = "run:"
LEASE_WAIT_SLICE = 25.0
KEEPALIVE_SECONDS = 30.0
BACKEND_TTL = "30m"
CassetteMode = Literal["record", "replay", "auto"]
log = logging.getLogger("eks_harness.engine.resources")
_BACKEND_KIND = "backend"
_POLLS_PER_QUEUE_TIMEOUT = 3.0
_MIN_LEASE_WAIT_SLICE = 0.05
_ANY_DEFINITION = "*"
_ALWAYS_ACTIVE = frozenset({BUILTIN_PLUGIN, PROJECT_PLUGIN})
_NO_BACKEND = frozenset({"none", "no", "off", "false"})
_CASSETTE_MODES: frozenset[str] = frozenset(get_args(CassetteMode))


SeedRestoreMode = Literal["restore", "fresh", "rebuild", "build"]


class SeedRestoreRequest(EngineModel):
    run: RunInfo
    resource_id: str
    session_key: str | None
    seed: str
    seed_hash: str | None
    backend: BackendIdentity
    url: str
    now: str | None
    key: str
    mode: SeedRestoreMode = "restore"


class SeedHook(Protocol):
    def restore(self, request: SeedRestoreRequest, info: Mapping[str, JsonValue], *,
                registry: Registry | None = None) -> SeedRestoreResult: ...


class CassetteRequest(EngineModel):
    run: RunInfo
    node_id: str
    name: str
    mode: CassetteMode
    key: str
    backend: BackendIdentity | None


class CassetteHook(Protocol):
    def available(self, request: CassetteRequest) -> bool: ...

    def opened(self, request: CassetteRequest) -> Mapping[str, JsonValue]: ...

    def closed(self, request: CassetteRequest, recorded: Mapping[str, JsonValue]) -> str | None: ...


class LeaseNeed(EngineModel):
    resource: Resource
    platform: str
    session_key: str
    backend_resource: str | None


class BackendNeed(EngineModel):
    resource: Resource
    session_key: str | None
    ref: BackendRef | None
    source: BackendSource | None
    seed: str | None
    seed_hash: str | None


class CassetteNeed(EngineModel):
    node_id: str
    name: str
    mode: CassetteMode
    session_key: str | None


class ResourcePlan(EngineModel):
    leases: list[LeaseNeed] = Field(default_factory=list[LeaseNeed])
    backends: list[BackendNeed] = Field(default_factory=list[BackendNeed])
    cassettes: list[CassetteNeed] = Field(default_factory=list[CassetteNeed])
    slots: list[Location] = Field(default_factory=list[Location])


@dataclass
class ResourceSink:
    record: Callable[[ResourceRecord], None]
    event: Callable[[str, Mapping[str, JsonValue]], None]


@dataclass(frozen=True, slots=True)
class BackendChoiceOut:
    definition: str
    source: BackendSource
    policy: str | None = None
    policy_plugin: str | None = None
    policy_version: str | None = None
    ref: BackendRef | None = None


def seed_key(seed_hash: str | None, backend: BackendIdentity) -> str:
    return digest(canonical_json({"seed": seed_hash, "backend": {
        "definition": backend.definition, "plugin": backend.plugin, "plugin_version": backend.plugin_version,
        "fingerprint": backend.fingerprint, "version": backend.version, "commit": backend.commit}}))


def with_seed_personas(secrets: Mapping[str, JsonValue], personas: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    merged: dict[str, JsonValue] = dict(secrets)
    credentials = merged.get("credentials")
    credentials = dict(credentials) if isinstance(credentials, Mapping) else {}
    known = credentials.get("personas")
    known = dict(known) if isinstance(known, Mapping) else {}
    known.update(personas)
    credentials["personas"] = known
    merged["credentials"] = credentials
    return merged


def cassette_key(plan: Plan, node: Node, backend: BackendIdentity | None) -> str:
    identity: dict[str, JsonValue] | None = None if backend is None else {
        "definition": backend.definition, "fingerprint": backend.fingerprint, "version": backend.version,
        "commit": backend.commit}
    return digest(canonical_json({"plan": plan.hash, "node": node.hash, "name": node.args.get("name"),
                                  "backend": identity}))


class SeedHooks:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hooks: list[tuple[str, SeedHook]] = []

    def register(self, definition: str, hook: SeedHook) -> None:
        if not definition:
            raise ValueError("register a seed hook for a backend definition name or '*'")
        with self._lock:
            self._hooks.append((definition, hook))

    def hook(self, definition: str) -> SeedHook | None:
        with self._lock:
            hooks = list(self._hooks)
        exact = [hook for name, hook in hooks if name == definition and name != _ANY_DEFINITION]
        if exact:
            return exact[-1]
        anywhere = [hook for name, hook in hooks if name == _ANY_DEFINITION]
        return anywhere[-1] if anywhere else None


class CassetteHooks:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hooks: list[CassetteHook] = []

    def register(self, hook: CassetteHook) -> None:
        with self._lock:
            self._hooks.append(hook)

    def hook(self) -> CassetteHook | None:
        with self._lock:
            return self._hooks[-1] if self._hooks else None


class SlotManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, location: Location, label: str, *, run_id: str, cancel: threading.Event,
                on_wait: Callable[[int, str], None] | None = None) -> SlotHandle:
        def waiting(position: int, ticket: renderq.Ticket) -> None:
            if on_wait is not None:
                on_wait(position, ticket.id)

        ticket = renderq.claim(location, label, session=run_id, cancel=cancel.is_set, on_wait=waiting)
        return SlotHandle(ticket=ticket.id, location=location, label=label,
                          acquired_at=ticket.started_at if ticket.started_at is not None else time.time())

    def release(self, slot: SlotHandle) -> None:
        renderq.drop(slot.ticket)


@dataclass
class HeldResources:
    resources: RunResources
    backend_leases: dict[str, str] = field(default_factory=dict[str, str])
    driver_sids: dict[str, str] = field(default_factory=dict[str, str])
    records: dict[str, ResourceRecord] = field(default_factory=dict[str, ResourceRecord])


@dataclass(frozen=True, slots=True)
class _DefinitionPlugin:
    plugin: str | None
    version: str | None
    active: bool


def _implementation(reference: str) -> object | None:
    module_name, _, qualname = reference.partition(":")
    if not module_name or not qualname:
        return None
    found: object | None = sys.modules.get(module_name)
    if found is None:
        try:
            found = importlib.import_module(module_name)
        except ImportError:
            return None
    for part in qualname.split("."):
        found = getattr(found, part, None)
        if found is None:
            return None
    return found


def _first_line(text: object) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0][:300] if lines else "no detail"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


class ResourcePlanner:
    def __init__(self, ctx: AppContext, *, seeds: SeedHooks, cassettes: CassetteHooks,
                 slots: SlotManager | None = None) -> None:
        self.ctx = ctx
        self.seeds = seeds
        self.cassettes = cassettes
        self.slots = slots or SlotManager()
        self._lock = threading.Lock()
        self._sinks: dict[str, ResourceSink] = {}
        self._runs: dict[str, RunInfo] = {}

    def plan(self, plan: Plan, *, registry: Registry, tree: str | None, sessions: Mapping[str, SessionSpec],
             backend: BackendRef | None = None, backends: Mapping[str, BackendRef] | None = None) -> ResourcePlan:
        from eks.workspace import seed_info

        chosen = dict(backends or {})
        backend_of_session: dict[str, str] = {}
        needs = ResourcePlan()
        for resource in plan.resources:
            if resource.kind != _BACKEND_KIND:
                continue
            key = resource.session or MAIN_SESSION
            backend_of_session.setdefault(key, resource.id)
            ref = chosen.get(key) or backend
            info = seed_info(resource.seed, registry=registry) if resource.seed else None
            source: BackendSource | None = "request" if ref is not None else ("plan" if resource.definition else None)
            needs.backends.append(BackendNeed(resource=resource, session_key=key, ref=ref, source=source,
                                              seed=resource.seed, seed_hash=info.hash if info is not None else None))
        for resource in plan.resources:
            if resource.kind not in LEASE_KIND_BY_RESOURCE:
                continue
            key = resource.session or MAIN_SESSION
            needs.leases.append(LeaseNeed(resource=resource, platform=resource.kind.split(".", 1)[1],
                                          session_key=key, backend_resource=backend_of_session.get(key)))
        for node, ancestors in plan.walk():
            if node.op == CASSETTE_OP:
                mode = node.args.get("mode", "auto")
                needs.cassettes.append(CassetteNeed(
                    node_id=node.id, name=str(node.args.get("name") or ""),
                    mode=cast(CassetteMode, mode) if isinstance(mode, str) and mode in _CASSETTE_MODES else "auto",
                    session_key=self._node_session(plan, node, ancestors)))
            if registry.has_op(node.op):
                location = registry.op(node.op).location
                if location in SLOT_LOCATIONS and location not in needs.slots:
                    needs.slots.append(location)
        return needs

    def _node_session(self, plan: Plan, node: Node, ancestors: tuple[Node, ...]) -> str | None:
        by_id = {resource.id: resource for resource in plan.resources}
        for candidate in (node, *reversed(ancestors)):
            for resource_id in candidate.uses:
                resource = by_id.get(resource_id)
                if resource is not None:
                    return resource.session or MAIN_SESSION
        return plan.session.key if plan.session is not None else None

    def choose_backend(self, need: BackendNeed, *, registry: Registry, tree: str | None,
                       session: SessionSpec | None) -> BackendChoiceOut:
        key = need.session_key or MAIN_SESSION
        if need.ref is not None:
            return BackendChoiceOut(definition=self._ref_definition(need.ref, tree), source="request", ref=need.ref)
        if need.resource.definition:
            return BackendChoiceOut(definition=need.resource.definition, source="plan")
        if tree is None:
            raise self._no_backend(key)
        root = Path(tree)
        app = session.app if session is not None else need.resource.app
        if app:
            profile = self._profile_choice(root, app, tree)
            if profile is not None:
                return profile
        policy = self._policy_choice(registry, root, tree)
        if policy is not None:
            return policy
        legacy = self._legacy_choice(registry, root, tree)
        if legacy is not None:
            return legacy
        default = self._project_default(root)
        if default is not None:
            return BackendChoiceOut(definition=default, source="project_default")
        raise self._no_backend(key)

    @staticmethod
    def _no_backend(key: str) -> ResourceError:
        return ResourceError(f"no backend for session {key}: name one with backend= on the flow or its seed, pass "
                             "--backend, or add a backend policy")

    def _resolve_ref(self, ref: BackendRef, tree: str | None) -> dict[str, Any]:
        from eks_harness.pools.base import BackendError

        try:
            return self.ctx.pools.backends.resolve_ref(ref.ref, tree=ref.tree or tree, key=ref.key,
                                                       params=dict(ref.params))
        except BackendError as error:
            raise ResourceError(f"backend {ref.ref}: {error}") from error

    def _ref_definition(self, ref: BackendRef, tree: str | None) -> str:
        definition = _text(self._resolve_ref(ref, tree).get("definition"))
        if not definition:
            raise ResourceError(f"backend {ref.ref} names no backend definition")
        return definition

    def _profile_choice(self, root: Path, app: str, tree: str) -> BackendChoiceOut | None:
        from eks.discovery import DiscoveryError, find_app

        try:
            found = find_app(root, app)
        except DiscoveryError as error:
            raise ResourceError(f"the app profile of {app} could not be read: {error}") from error
        table = found.tables.get("backend") if found is not None else None
        if found is None or not isinstance(table, Mapping):
            return None
        entries = cast(Mapping[str, Any], table)
        setting = "link" if entries.get("link") else "definition"
        reference = entries.get(setting)
        if not isinstance(reference, str) or not reference:
            return None
        if reference.strip().lower() in _NO_BACKEND:
            raise ResourceError(f"the app profile of {app} turns the backend off ([backend] {setting} = {reference}); "
                                "pass --backend or remove the setting")
        backend_tree = entries.get("tree")
        resolved_tree: str | None = None
        if isinstance(backend_tree, str) and backend_tree:
            path = Path(backend_tree).expanduser()
            resolved_tree = str(path if path.is_absolute() else (found.root / path).resolve())
        params = entries.get("params")
        key = entries.get("key")
        ref = BackendRef(ref=reference, tree=resolved_tree, key=key if isinstance(key, str) and key else None,
                         params={str(k): str(v) for k, v in cast(Mapping[str, Any], params).items()}
                         if isinstance(params, Mapping) else {})
        return BackendChoiceOut(definition=self._ref_definition(ref, tree), source="profile", ref=ref)

    def _known_definition(self, name: str, registry: Registry, tree: str) -> bool:
        from eks_harness.plugins import PluginError
        from eks_harness.plugins.project import ProjectConfigError
        from eks_harness.pools.base import BackendError

        try:
            registry.backend(name)
        except RegistryError:
            pass
        else:
            return True
        try:
            listed = self.ctx.pools.backends.definitions(tree)
        except (BackendError, PluginError, ProjectConfigError) as error:
            raise ResourceError(f"the backend definitions of {tree} could not be listed: {error}") from error
        if any(item.get("name") == name for item in listed):
            return True
        return self._definition_plugin(name, registry, tree).plugin is not None

    def _require_definition(self, decided: str, policy: str, target: str, reason: str, registry: Registry,
                            tree: str) -> None:
        if self._known_definition(decided, registry, tree):
            return
        raise ResourceError(
            f"backend policy {policy} decided {decided} (target {target or 'none'}: {reason or 'no reason given'}), "
            f"which is not a backend definition of {tree}; the engine only leases managed backends: pass --backend "
            "<definition>, or name one with backend= on the flow or its seed")

    def _policy_choice(self, registry: Registry, root: Path, tree: str) -> BackendChoiceOut | None:
        for spec in registry.backend_policies():
            impl = _implementation(spec.impl)
            if not callable(impl):
                raise ResourceError(f"backend policy {spec.name}: {spec.impl} cannot be loaded")
            factory = cast(Callable[[], Any], impl)
            try:
                choice = factory().choose(root, None)
            except Exception as error:
                raise ResourceError(f"backend policy {spec.name} failed: {error}") from error
            if not isinstance(choice, BackendChoice):
                raise ResourceError(f"backend policy {spec.name} returned {type(choice).__name__}, not a "
                                    "BackendChoice")
            if choice.backend:
                self._require_definition(choice.backend, spec.name, choice.target, choice.reason, registry, tree)
                return BackendChoiceOut(definition=choice.backend, source="policy", policy=spec.name,
                                        policy_plugin=spec.provenance.plugin,
                                        policy_version=spec.provenance.version or None)
        return None

    def _legacy_choice(self, registry: Registry, root: Path, tree: str) -> BackendChoiceOut | None:
        from eks_harness.backends.registry import choose_backend
        from eks_harness.plugins import PluginError
        from eks_harness.plugins.project import ProjectConfigError

        try:
            decided = choose_backend(self.ctx.plugins, root)
        except (PluginError, ProjectConfigError) as error:
            raise ResourceError(f"the backend policy plugins of {root} could not be loaded: {error}") from error
        policy = _text(decided.get("policy"))
        definition = _text(decided.get("backend"))
        if not policy or not definition:
            candidates = decided.get("candidates")
            for candidate in cast(list[Any], candidates) if isinstance(candidates, list) else []:
                if isinstance(candidate, Mapping) and "error" in candidate:
                    failed = cast(Mapping[str, Any], candidate)
                    raise ResourceError(f"backend policy {_text(failed.get('policy'))} failed: "
                                        f"{_text(failed.get('error')) or 'no detail'}")
            return None
        self._require_definition(definition, policy, _text(decided.get("target")), _text(decided.get("reason")),
                                 registry, tree)
        plugin = policy.split(":", 1)[0]
        record = self.ctx.plugins.get(plugin, root)
        version = record.manifest.version if record is not None and record.manifest is not None else None
        return BackendChoiceOut(definition=definition, source="legacy_policy", policy=policy, policy_plugin=plugin,
                                policy_version=version or None)

    @staticmethod
    def _project_default(root: Path) -> str | None:
        from eks_harness.plugins.project import ProjectConfigError, load_project_config

        try:
            configured = load_project_config(root).section("backends").get("default")
        except ProjectConfigError as error:
            raise ResourceError(f"the project file of {root} could not be read: {error}") from error
        return str(configured) if configured else None

    def _definition_plugin(self, definition: str, registry: Registry, tree: str | None) -> _DefinitionPlugin:
        try:
            spec = registry.backend(definition)
        except RegistryError:
            spec = None
        if spec is not None:
            return _DefinitionPlugin(spec.provenance.plugin, spec.provenance.version, True)
        from eks_harness.plugins.project import ProjectConfigError

        try:
            records = self.ctx.plugins.records(Path(tree) if tree else None)
        except ProjectConfigError as error:
            raise ResourceError(f"the plugins of {tree} could not be listed: {error}") from error
        inactive: _DefinitionPlugin | None = None
        for record in records:
            manifest = record.manifest
            if manifest is None or record.state == "shadowed":
                continue
            if not any(item.id == definition for item in manifest.of_type(_BACKEND_KIND)):
                continue
            if record.active:
                return _DefinitionPlugin(manifest.id, manifest.version, True)
            inactive = inactive or _DefinitionPlugin(manifest.id, manifest.version, False)
        return inactive or _DefinitionPlugin(None, None, True)

    @staticmethod
    def _check_plugin(plan: Plan, choice: BackendChoiceOut, owner: _DefinitionPlugin, tree: str | None) -> None:
        if owner.plugin is not None and not owner.active and owner.plugin not in _ALWAYS_ACTIVE:
            chooser = f"backend policy {choice.policy}" if choice.policy else f"the {choice.source} source"
            raise ResourceError(
                f"{chooser} chose backend {choice.definition}, but its plugin {owner.plugin} is not active in "
                f"{tree or 'the run'}; trust and enable the plugin or choose another backend")
        for requirement in plan.requires:
            if owner.plugin is not None and requirement.plugin == owner.plugin \
                    and requirement.version != (owner.version or ""):
                raise ResourceError(
                    f"the plan was built with plugin {owner.plugin} {requirement.version}, but backend "
                    f"{choice.definition} comes from {owner.plugin} {owner.version or '(no version)'}; plan the "
                    "flow again or install the plugin version it was built with")

    def acquire(self, run: RunInfo, plan: Plan, needs: ResourcePlan, *, registry: Registry,
                sessions: Mapping[str, SessionSpec], sink: ResourceSink, cancel: threading.Event,
                seed_mode: SeedMode = "restore", seed_hook: SeedHook | None = None,
                cassette_hook: CassetteHook | None = None) -> HeldResources:
        held = HeldResources(resources=RunResources())
        with self._lock:
            self._sinks[run.id] = sink
            self._runs[run.id] = run
        current: list[str] = []
        try:
            for need in needs.backends:
                self._check_cancel(cancel)
                self._acquire_backend(run, plan, need, registry, sessions, sink, cancel, held, current)
            if seed_mode != "skip":
                for need in needs.backends:
                    if need.seed:
                        self._check_cancel(cancel)
                        self._restore_seed(run, plan, need, sessions, sink, held, seed_hook, current, seed_mode,
                                           registry)
            for index, need in enumerate(needs.leases):
                self._check_cancel(cancel)
                self._acquire_lease(run, need, needs.leases[:index], sink, cancel, held, current)
            for need in needs.cassettes:
                if need.mode == "replay":
                    self._check_cancel(cancel)
                    self._check_cassette(run, plan, need, sink, held, cassette_hook, current)
        except BaseException as error:
            message = error.message if isinstance(error, ResourceError) else f"{type(error).__name__}: {error}"
            if current and held.records[current[-1]].state in ("pending", "acquiring"):
                self._mark(held, sink, current[-1], state="failed", error=message)
            self.release(held, f"acquisition failed: {message}", sink)
            raise
        return held

    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise ResourceError("cancelled")

    def _start(self, held: HeldResources, sink: ResourceSink, current: list[str], record: ResourceRecord) -> None:
        current.append(record.resource_id)
        held.records[record.resource_id] = record
        sink.record(record)

    def _mark(self, held: HeldResources, sink: ResourceSink, resource_id: str, **changes: Any) -> ResourceRecord:
        record = held.records[resource_id].model_copy(update=changes)
        held.records[resource_id] = record
        sink.record(record)
        return record

    def _acquire_backend(self, run: RunInfo, plan: Plan, need: BackendNeed, registry: Registry,
                         sessions: Mapping[str, SessionSpec], sink: ResourceSink, cancel: threading.Event,
                         held: HeldResources, current: list[str]) -> None:
        from eks_harness.pools.base import BackendError

        resource_id = need.resource.id
        self._start(held, sink, current, ResourceRecord(run_id=run.id, resource_id=resource_id, kind="backend",
                                                         session_key=need.session_key, state="pending"))
        session = sessions.get(need.session_key) if need.session_key else None
        choice = self.choose_backend(need, registry=registry, tree=run.tree, session=session)
        owner = self._definition_plugin(choice.definition, registry, run.tree)
        self._check_plugin(plan, choice, owner, run.tree)
        self._mark(held, sink, resource_id, state="acquiring",
                   detail={"definition": choice.definition, "source": choice.source, "policy": choice.policy})
        backends = self.ctx.pools.backends
        target: dict[str, Any] = self._resolve_ref(choice.ref, run.tree) if choice.ref is not None else {
            "definition": choice.definition}
        body: dict[str, Any] = {
            "definition": _text(target.get("definition")) or choice.definition,
            "tree": target.get("tree") or run.tree, "key": target.get("key"), "params": target.get("params") or {},
            "kind": "agent", "holder": f"run {run.id}", "owner": f"{RUN_INSTANCE_PREFIX}{run.id}",
            "project": run.project, "session": run.session, "ttl": BACKEND_TTL, "userId": run.user_id}
        if target.get("id"):
            body["id"] = target["id"]
        if target.get("instance"):
            body["instance"] = target["instance"]
        try:
            response = backends.acquire(body)
        except BackendError as error:
            raise ResourceError(f"backend {choice.definition} for session {need.session_key}: {error}") from error
        lease = response.get("lease") or {}
        record = response.get("backend") or {}
        lease_id, backend_id = _text(lease.get("id")), _text(record.get("id"))
        if lease_id:
            held.backend_leases[resource_id] = lease_id
        if not lease_id or not backend_id:
            raise ResourceError(f"backend {choice.definition} gave the run no lease: "
                                f"{_first_line(response.get('error') or response.get('status'))}")
        timeout = float(self.ctx.config["backend.readyTimeoutSeconds"])
        status = backends.wait_ready(backend_id, timeout, abort=cancel.is_set)
        self._check_cancel(cancel)
        if status == "failed":
            failed = backends.get(backend_id) or {}
            raise ResourceError(f"backend {backend_id} failed: {_first_line(failed.get('error'))}")
        if status != "running":
            raise ResourceError(f"backend {backend_id} was not ready within {timeout:.0f}s (status {status})")
        try:
            info = backends.info(backend_id)
            secrets = backends.info(backend_id, secrets=True)
        except BackendError as error:
            raise ResourceError(f"backend {backend_id}: {error}") from error
        state = backends.get(backend_id) or {}
        identity = BackendIdentity(
            definition=_text(state.get("definition")) or choice.definition, backend_id=backend_id, lease_id=lease_id,
            source=choice.source, policy=choice.policy, policy_plugin=choice.policy_plugin,
            policy_version=choice.policy_version, plugin=owner.plugin, plugin_version=owner.version,
            fingerprint=str(state.get("fingerprint") or ""), commit=_text(secrets.get("commit")),
            branch=_text(secrets.get("branch")), version=_text(secrets.get("version")),
            generation=int(state.get("generation") or 0))
        url = _text(info.get("url")) or _text(secrets.get("url"))
        held.resources.backends[resource_id] = BackendHandle(resource_id=resource_id, session_key=need.session_key,
                                                             seed=need.seed, url=url, identity=identity, info=info,
                                                             secrets=secrets)
        self._mark(held, sink, resource_id, state="held", handle=lease_id, acquired_at=time.time(),
                   detail={**identity.model_dump(mode="json"), "url": url})

    def _restore_seed(self, run: RunInfo, plan: Plan, need: BackendNeed, sessions: Mapping[str, SessionSpec],
                      sink: ResourceSink, held: HeldResources, seed_hook: SeedHook | None,
                      current: list[str], seed_mode: SeedMode = "restore", registry: Registry | None = None) -> None:
        seed = need.seed or ""
        backend_id = need.resource.id
        resource_id = f"seed:{backend_id}"
        self._start(held, sink, current, ResourceRecord(run_id=run.id, resource_id=resource_id, kind="seed",
                                                         session_key=need.session_key, state="pending"))
        handle = held.resources.backends[backend_id]
        identity = handle.identity
        if need.seed_hash is None:
            raise ResourceError(f"seed {seed} is not registered in {run.tree or 'the run registry'}; register it with "
                                "@eks.seed or run with seed=skip")
        hook = seed_hook or self.seeds.hook(identity.definition)
        if hook is None:
            raise ResourceError(f"no seed engine can restore the seed {seed} into the backend {identity.definition}; "
                                "run with seed=skip to use the backend as it is, or with fakes to simulate the restore")
        key = seed_key(need.seed_hash, identity)
        self._mark(held, sink, resource_id, state="acquiring", handle=key,
                   detail={"seed": seed, "seedHash": need.seed_hash, "backend": identity.backend_id})
        session = sessions.get(need.session_key) if need.session_key else None
        builds = plan.kind == SEED_PLAN_KIND and plan.name == seed
        mode: SeedRestoreMode = "build" if builds else cast(SeedRestoreMode, seed_mode)
        request = SeedRestoreRequest(run=run, resource_id=backend_id, session_key=need.session_key, seed=seed,
                                     seed_hash=need.seed_hash, backend=identity, url=handle.url,
                                     now=(session.now if session is not None else None) or plan.clock.now, key=key,
                                     mode=mode)
        try:
            result = hook.restore(request, handle.secrets, registry=registry)
        except ResourceError:
            raise
        except Exception as error:
            raise ResourceError(f"seed {seed} could not be restored into backend {identity.backend_id}: "
                                f"{type(error).__name__}: {error}") from error
        held.resources.seeds.append(result)
        if result.personas:
            held.resources.backends[backend_id] = handle.model_copy(
                update={"secrets": with_seed_personas(handle.secrets, result.personas)})
        self._mark(held, sink, resource_id, state="held", handle=result.key, acquired_at=time.time(),
                   detail={"seed": seed, "seedHash": need.seed_hash, "backend": identity.backend_id,
                           "restored": result.restored, "snapshot": result.snapshot, "mode": mode,
                           **result.detail})

    def _acquire_lease(self, run: RunInfo, need: LeaseNeed, earlier: Sequence[LeaseNeed], sink: ResourceSink,
                       cancel: threading.Event, held: HeldResources, current: list[str]) -> None:
        from eks_harness.daemon.leases import manager
        from eks_harness.pools.backends import pid_started

        resource = need.resource
        kind = LEASE_KIND_BY_RESOURCE[resource.kind]
        self._start(held, sink, current, ResourceRecord(run_id=run.id, resource_id=resource.id,
                                                         kind=cast(ResourceKind, resource.kind),
                                                         session_key=need.session_key, state="pending"))
        pid = os.getpid()
        started = pid_started(pid)
        instance = f"{RUN_INSTANCE_PREFIX}{run.id}"
        if any(LEASE_KIND_BY_RESOURCE[other.resource.kind] == kind for other in earlier):
            instance = f"{instance}/{resource.id}"
        body: dict[str, Any] = {
            "kind": kind, "project": run.project, "session": run.session, "instance": instance, "owner_pid": pid,
            "owner_started": started, "tree": run.tree, "label": f"run {run.id}",
            "tags": [f"{RUN_INSTANCE_PREFIX}{run.id}"],
            "claim": {"run": run.id, "pid": pid, "started": started, "host": socket.gethostname()}}
        backend_lease = held.backend_leases.get(need.backend_resource) if need.backend_resource else None
        if backend_lease:
            body["backend"] = backend_lease
        self._mark(held, sink, resource.id, state="acquiring", detail={"kind": kind, "instance": instance})
        leases = manager(self.ctx)
        actor = f"{RUN_INSTANCE_PREFIX}{run.id}"
        wait_slice = self._lease_wait_slice()
        try:
            response = leases.acquire(body, actor, wait=True, wait_seconds=wait_slice)
            sid = _text(response.get("sid"))
            if sid:
                held.driver_sids[resource.id] = sid
            lease_id = _text((response.get("lease") or {}).get("id"))
            while response.get("status") not in ("granted", "failed"):
                if cancel.is_set():
                    raise ResourceError("cancelled")
                position = response.get("position")
                self._mark(held, sink, resource.id, detail={"kind": kind, "instance": instance, "sid": sid,
                                                            "status": response.get("status"),
                                                            "position": position})
                self._poll_lease(leases, sid)
                response = leases.wait_for(lease_id, True, wait_slice)
        except ApiError as error:
            raise ResourceError(f"{kind} lease for session {need.session_key}: {error.message}") from error
        if response.get("status") == "failed":
            raise ResourceError(f"{kind} lease {sid} for session {need.session_key} failed: "
                                f"{_first_line(response.get('error'))}")
        handle = LeaseHandle(resource_id=resource.id, kind=cast(ResourceKind, resource.kind),
                             platform=cast(Any, need.platform), session_key=need.session_key, app=resource.app,
                             sid=sid, resource=_text(response.get("resource")) or None, project=run.project,
                             session=run.session)
        held.resources.leases[resource.id] = handle
        self._mark(held, sink, resource.id, state="held", handle=sid, acquired_at=time.time(),
                   detail={"kind": kind, "instance": instance, "sid": sid, "resource": handle.resource,
                           "backendLease": backend_lease})

    def _lease_wait_slice(self) -> float:
        queue_timeout = float(self.ctx.config["lease.queueTimeoutSeconds"])
        return max(_MIN_LEASE_WAIT_SLICE, min(LEASE_WAIT_SLICE, queue_timeout / _POLLS_PER_QUEUE_TIMEOUT))

    @staticmethod
    def _poll_lease(leases: LeaseManager, sid: str) -> None:
        try:
            leases.heartbeat(sid)
        except ApiError as error:
            if error.error != "lease_released":
                raise

    def _check_cassette(self, run: RunInfo, plan: Plan, need: CassetteNeed, sink: ResourceSink, held: HeldResources,
                        cassette_hook: CassetteHook | None, current: list[str]) -> None:
        resource_id = f"cassette:{need.node_id}"
        self._start(held, sink, current, ResourceRecord(run_id=run.id, resource_id=resource_id, kind="cassette",
                                                         session_key=need.session_key, state="pending"))
        backend = self._session_backend(held, need.session_key)
        identity = backend.identity if backend is not None else None
        key = cassette_key(plan, plan.node(need.node_id), identity)
        self._mark(held, sink, resource_id, state="acquiring", handle=key, detail={"name": need.name, "mode": need.mode})
        if not self.cassette_available(run, plan, need, identity, cassette_hook):
            raise ResourceError(f"cassette_missing: the replay cassette {need.name} of node {need.node_id} was never "
                                "recorded for this plan and backend; run it once with mode record or auto",
                                detail={"code": "cassette_missing", "node": need.node_id, "name": need.name,
                                        "key": key})
        self._mark(held, sink, resource_id, state="held", acquired_at=time.time(),
                   detail={"name": need.name, "mode": need.mode,
                           "backend": identity.backend_id if identity is not None else None})

    @staticmethod
    def _session_backend(held: HeldResources, session_key: str | None) -> BackendHandle | None:
        handles = list(held.resources.backends.values())
        matching = [handle for handle in handles if handle.session_key == session_key]
        if matching:
            return matching[0]
        return handles[0] if len(handles) == 1 else None

    def _run_of(self, held: HeldResources) -> tuple[RunInfo | None, ResourceSink | None]:
        run_id = next((record.run_id for record in held.records.values()), None)
        if run_id is None:
            return None, None
        with self._lock:
            return self._runs.get(run_id), self._sinks.get(run_id)

    def keepalive(self, held: HeldResources) -> None:
        from eks_harness.daemon.leases import manager
        from eks_harness.pools.base import BackendError, BackendLeaseGone

        run, sink = self._run_of(held)
        leases = manager(self.ctx)
        leases.last_activity = time.time()
        run_id = run.id if run is not None else next((record.run_id for record in held.records.values()), "?")
        for resource_id, handle in list(held.resources.leases.items()):
            try:
                leases.heartbeat(handle.sid)
            except ApiError as error:
                self._lost(held, sink, resource_id, "lease", handle.sid, error.message)
            except Exception:
                log.exception("run %s: lease %s could not be heartbeated", run_id, handle.sid)
        for resource_id, handle in list(held.resources.backends.items()):
            lease_id = handle.identity.lease_id
            try:
                self.ctx.pools.backends.renew(lease_id, BACKEND_TTL)
            except BackendLeaseGone as error:
                self._lost(held, sink, resource_id, "backend", lease_id, str(error))
            except BackendError as error:
                log.warning("run %s: backend lease %s could not be renewed: %s", run_id, lease_id, error)
            except Exception:
                log.exception("run %s: backend lease %s could not be renewed", run_id, lease_id)

    @staticmethod
    def _lost(held: HeldResources, sink: ResourceSink | None, resource_id: str, kind: str, handle: str,
              reason: str) -> None:
        held.resources.leases.pop(resource_id, None)
        held.resources.backends.pop(resource_id, None)
        if sink is not None:
            sink.event("resource.lost", {"resource": resource_id, "kind": kind, "handle": handle, "reason": reason})

    def release(self, held: HeldResources, reason: str, sink: ResourceSink) -> None:
        from eks_harness.daemon.leases import manager

        run, _ = self._run_of(held)
        run_id = run.id if run is not None else next((record.run_id for record in held.records.values()), "")
        released_at = time.time()
        leases = manager(self.ctx)
        for resource_id, sid in list(held.driver_sids.items()):
            try:
                leases.release(sid, reason, actor=f"{RUN_INSTANCE_PREFIX}{run_id}")
            except Exception as error:
                log.exception("run %s: lease %s could not be released", run_id, sid)
                self._released(held, sink, resource_id, released_at, f"release failed: {error}")
                continue
            self._released(held, sink, resource_id, released_at, None)
            held.driver_sids.pop(resource_id, None)
        for resource_id, lease_id in list(held.backend_leases.items()):
            try:
                self.ctx.pools.backends.release(lease_id, reason)
            except Exception as error:
                log.exception("run %s: backend lease %s could not be released", run_id, lease_id)
                self._released(held, sink, resource_id, released_at, f"release failed: {error}")
                continue
            self._released(held, sink, resource_id, released_at, None)
            held.backend_leases.pop(resource_id, None)
        for resource_id, record in list(held.records.items()):
            if record.state == "held":
                self._released(held, sink, resource_id, released_at, None)
        with self._lock:
            self._sinks.pop(run_id, None)
            self._runs.pop(run_id, None)

    @staticmethod
    def _released(held: HeldResources, sink: ResourceSink, resource_id: str, released_at: float,
                  error: str | None) -> None:
        record = held.records.get(resource_id)
        if record is None or record.state in ("released", "failed", "pending"):
            return
        updated = record.model_copy(update={"state": "released", "released_at": released_at,
                                            **({"error": error} if error else {})})
        held.records[resource_id] = updated
        try:
            sink.record(updated)
        except Exception:
            log.exception("run %s: the release of %s could not be journaled", record.run_id, resource_id)

    def backend_handle(self, held: HeldResources, resource_id: str) -> BackendHandle | None:
        return held.resources.backend(resource_id)

    def lease_handle(self, held: HeldResources, resource_id: str) -> LeaseHandle | None:
        return held.resources.lease(resource_id)

    def cassette_available(self, run: RunInfo, plan: Plan, need: CassetteNeed, backend: BackendIdentity | None,
                           hook: CassetteHook | None = None) -> bool:
        chosen = hook or self.cassettes.hook()
        if chosen is None:
            return False
        request = CassetteRequest(run=run, node_id=need.node_id, name=need.name, mode=need.mode,
                                  key=cassette_key(plan, plan.node(need.node_id), backend), backend=backend)
        return bool(chosen.available(request))


def lease_kinds(plan: Plan) -> Sequence[str]:
    kinds: list[str] = []
    for resource in plan.resources:
        kind = LEASE_KIND_BY_RESOURCE.get(resource.kind)
        if kind is not None and kind not in kinds:
            kinds.append(kind)
    return kinds


__all__ = [
    "BACKEND_TTL",
    "CASSETTE_OP",
    "KEEPALIVE_SECONDS",
    "LEASE_KIND_BY_RESOURCE",
    "LEASE_WAIT_SLICE",
    "RUN_INSTANCE_PREFIX",
    "SLOT_LOCATIONS",
    "BackendChoiceOut",
    "BackendNeed",
    "CassetteHook",
    "CassetteHooks",
    "CassetteMode",
    "CassetteNeed",
    "CassetteRequest",
    "HeldResources",
    "LeaseNeed",
    "ResourcePlan",
    "ResourcePlanner",
    "ResourceSink",
    "SeedHook",
    "SeedHooks",
    "SeedRestoreMode",
    "SeedRestoreRequest",
    "SlotManager",
    "cassette_key",
    "lease_kinds",
    "seed_key",
    "with_seed_personas",
]
