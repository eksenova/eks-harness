from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from eks_harness.data.build import BuildTarget, SeedBuild, outputs_digest
from eks_harness.data.context import SeedError
from eks_harness.data.store import Divergence, Manifest, SnapshotStore, snapshot_name
from eks_harness.engine.auth import AuthRequest, PersonaAuthRegistry
from eks_harness.engine.errors import PersonaAuthError, ResourceError
from eks_harness.engine.model import (
    BackendHandle,
    BackendIdentity,
    SeedRestoreResult,
)
from eks_harness.engine.resources import SeedRestoreRequest, with_seed_personas
from eks_harness.ir.hashing import canonical_json, digest
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry, RegistryError

if TYPE_CHECKING:
    from eks_harness.daemon.context import AppContext

log = logging.getLogger("eks_harness.data")
LOG_LINES = 60
DIVERGENT_PATHS = 20
SEED_RECORD_FIELD = "seed"


def snapshot_key(seed_hash: str | None, identity: BackendIdentity, migrations: str | None) -> str:
    backend: dict[str, JsonValue] = {"definition": identity.definition, "plugin": identity.plugin,
                                     "plugin_version": identity.plugin_version}
    if migrations:
        backend["migrations"] = migrations
    else:
        backend.update({"fingerprint": identity.fingerprint, "version": identity.version, "commit": identity.commit})
    return digest(canonical_json({"seed": seed_hash, "backend": backend}))


def planned_seed(name: str, registry: Registry) -> Plan | None:
    from eks.workspace import planned_seed as plan_of

    return plan_of(name, registry=registry)


class SeedEngine:
    def __init__(self, ctx: AppContext, auth: PersonaAuthRegistry, *, store: SnapshotStore | None = None,
                 keep: int | None = None) -> None:
        self.ctx = ctx
        self.auth = auth
        self.store = store or SnapshotStore(ctx.paths.state_dir / "seeds")
        self.keep = keep
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._builds: dict[tuple[str, str], SeedBuild] = {}
        self._held: dict[str, list[threading.Lock]] = {}

    def _keep(self) -> int:
        if self.keep is not None:
            return self.keep
        try:
            return max(1, int(self.ctx.config["seeds.keepSnapshots"]))
        except (KeyError, TypeError, ValueError):
            return 3

    def _lock_for(self, backend_id: str) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(backend_id, threading.Lock())

    def step(self, target: BuildTarget, step: str, *, name: str | None = None, phase: str | None = None,
             required: bool = False) -> dict[str, Any]:
        from eks_harness.pools.base import BackendError

        try:
            answer = self.ctx.pools.backends.step(target.backend_id, step, name=name, phase=phase)
        except BackendError as error:
            raise SeedError(f"the {step} hook of backend {target.backend_id} failed: "
                            f"{str(error).splitlines()[0] if str(error) else error!r}") from error
        if answer.get("missing") and required:
            raise SeedError(f"backend {target.definition} implements no {step} hook, which a seed build needs to "
                            "start from an empty store")
        return answer

    def migrations(self, target: BuildTarget) -> str | None:
        answer = self.step(target, "migrations")
        value = answer.get("migrations")
        return str(value) if isinstance(value, str) and value else None

    def is_persona(self, registry: Registry, kind: str) -> bool:
        try:
            return registry.resource(kind).persona
        except RegistryError:
            return False

    def persona_headers(self, target: BuildTarget, personas: Mapping[str, JsonValue],
                        persona: str) -> Mapping[str, str]:
        handle = target.handle.model_copy(update={"secrets": with_seed_personas(target.handle.secrets, personas)})
        request = AuthRequest(persona=persona, backend=handle, session=None, run=target.run,
                              registry=target.registry, tree=target.tree)
        try:
            return self.auth.headers(request)
        except PersonaAuthError as error:
            raise SeedError(str(error)) from error

    def _record_seed(self, target: BuildTarget, value: dict[str, Any] | None) -> None:
        backends = self.ctx.pools.backends
        with backends.host.lock:
            record = backends.get(target.backend_id)
            if record is None:
                return
            if value is None:
                record.pop(SEED_RECORD_FIELD, None)
            else:
                record[SEED_RECORD_FIELD] = value
            backends.host.save()

    def _holds(self, target: BuildTarget, key: str) -> bool:
        record = self.ctx.pools.backends.get(target.backend_id) or {}
        current = record.get(SEED_RECORD_FIELD) or {}
        return current.get("key") == key and int(current.get("generation") or -1) == int(record.get("generation") or 0)

    def _mark_held(self, target: BuildTarget, manifest: Manifest) -> None:
        record = self.ctx.pools.backends.get(target.backend_id) or {}
        self._record_seed(target, {"key": manifest.key, "seed": manifest.seed,
                                   "generation": int(record.get("generation") or 0)})

    def _target(self, request: SeedRestoreRequest, info: Mapping[str, JsonValue],
                registry: Registry | None) -> BuildTarget:
        if registry is None:
            raise ResourceError(f"seed {request.seed}: the seed engine needs the run's registry to plan the seed")
        handle = BackendHandle(resource_id=request.resource_id, session_key=request.session_key, seed=request.seed,
                               url=request.url, identity=request.backend, info={}, secrets=dict(info))
        return BuildTarget(run=request.run, handle=handle, registry=registry,
                           tree=Path(request.run.tree) if request.run.tree else None)

    def restore(self, request: SeedRestoreRequest, info: Mapping[str, JsonValue], *,
                registry: Registry | None = None) -> SeedRestoreResult:
        target = self._target(request, info, registry)
        lines: deque[str] = deque(maxlen=LOG_LINES)

        def note(message: str) -> None:
            lines.append(message)
            log.info("seed %s on %s: %s", request.seed, target.backend_id, message)

        lock = self._lock_for(target.backend_id)
        lock.acquire()
        try:
            plan = planned_seed(request.seed, target.registry)
            if plan is None:
                raise ResourceError(f"seed {request.seed} is not registered in {request.run.tree or 'the registry'}")
            migrations = self.migrations(target)
            key = snapshot_key(plan.hash, target.handle.identity, migrations)
            if request.mode == "build":
                build = SeedBuild(self, target, seed=request.seed, plan=plan, key=key, migrations=migrations,
                                  log=note)
                self._record_seed(target, None)
                build.begin()
                self._builds[(request.run.id, request.resource_id)] = build
                self._held.setdefault(request.run.id, []).append(lock)
                lock = None
                return SeedRestoreResult(resource_id=request.resource_id, seed=request.seed, key=key, restored=False,
                                         detail={"building": True, "migrations": migrations})
            manifest = None if request.mode == "rebuild" else self.store.get(target.definition, key)
            if manifest is not None and request.mode == "restore" and self._holds(target, key):
                return self._result(request, manifest, restored=False, detail={"reused": True}, lines=lines)
            if manifest is not None and manifest.snapshot:
                diverged = self._load(target, manifest, note)
                detail: dict[str, JsonValue] = {"built": False}
                if diverged:
                    detail["replayDiverged"] = list[JsonValue](diverged[:DIVERGENT_PATHS])
                return self._result(request, manifest, restored=True, detail=detail, lines=lines)
            manifest = self._build(target, request.seed, plan, key, migrations, note)
            return self._result(request, manifest, restored=True,
                                detail={"built": True, "deterministic": manifest.deterministic,
                                        "buildMs": manifest.build_ms, "resources": manifest.resources}, lines=lines)
        except SeedError as error:
            self._record_seed(target, None)
            raise ResourceError(f"seed {request.seed} on {target.backend_id}: {error.message}") from error
        finally:
            if lock is not None:
                lock.release()

    def _result(self, request: SeedRestoreRequest, manifest: Manifest, *, restored: bool,
                detail: dict[str, JsonValue], lines: deque[str]) -> SeedRestoreResult:
        return SeedRestoreResult(resource_id=request.resource_id, seed=request.seed, key=manifest.key,
                                 restored=restored, snapshot=manifest.snapshot,
                                 detail={**detail, "personas": list[JsonValue](sorted(manifest.personas)),
                                         "resources": manifest.resources, "log": list[JsonValue](lines)},
                                 personas=dict(manifest.personas))

    def _load(self, target: BuildTarget, manifest: Manifest, note: Callable[[str], None]) -> list[str]:
        note(f"restoring snapshot {manifest.snapshot}")
        answer = self.step(target, "restore", name=manifest.snapshot)
        if answer.get("missing"):
            raise SeedError(f"backend {target.definition} implements no restore hook")
        diverged: list[str] = []
        if manifest.replay:
            plan = planned_seed(manifest.seed, target.registry) or Plan.model_construct()
            replayer = SeedBuild(self, target, seed=manifest.seed, plan=plan, key=manifest.key,
                                 migrations=manifest.migrations, log=note)
            replayer.state.personas.update(manifest.personas)
            try:
                diverged = replayer.replay(manifest.replay, manifest.outputs)
            finally:
                replayer.close()
        self._mark_held(target, manifest)
        return diverged

    def _build(self, target: BuildTarget, seed: str, plan: Plan, key: str, migrations: str | None,
               note: Callable[[str], None]) -> Manifest:
        note(f"building seed {seed}")
        self._record_seed(target, None)
        build = SeedBuild(self, target, seed=seed, plan=plan, key=key, migrations=migrations, log=note)
        try:
            build.run_plan()
            return self._snapshot(build)
        finally:
            build.close()

    def ensure_base(self, target: BuildTarget, base: str, *, log: Callable[[str], None]) -> Manifest:
        plan = planned_seed(base, target.registry)
        if plan is None:
            raise SeedError(f"base seed {base} is not registered")
        migrations = self.migrations(target)
        key = snapshot_key(plan.hash, target.handle.identity, migrations)
        manifest = self.store.get(target.definition, key)
        if manifest is not None and manifest.snapshot:
            self._load(target, manifest, log)
            return manifest
        return self._build(target, base, plan, key, migrations, log)

    def _snapshot(self, build: SeedBuild) -> Manifest:
        target = build.target
        name = snapshot_name(build.key)
        answer = self.step(target, "snapshot", name=name)
        saved = not answer.get("missing")
        digest_value = answer.get("digest") if isinstance(answer.get("digest"), str) else None
        manifest = build.manifest(snapshot=name if saved else None, digest_value=digest_value)
        previous = self.store.get(target.definition, build.key)
        if previous is not None:
            manifest = manifest.model_copy(update=_compare(previous, manifest))
            if previous.runs:
                manifest = manifest.model_copy(update={"runs": [*previous.runs[-9:], *manifest.runs]})
        if saved:
            self.store.put(manifest)
            self._prune(target, manifest)
            self._mark_held(target, manifest)
        else:
            build.log(f"backend {target.definition} implements no snapshot hook; the seed is built on every restore")
            self._record_seed(target, None)
        build.log(f"seed {build.seed} built: {manifest.resources} resources in {manifest.build_ms} ms")
        return manifest

    def _prune(self, target: BuildTarget, manifest: Manifest) -> None:
        for old in self.store.stale(target.definition, manifest.seed, self._keep(), manifest.key):
            try:
                if old.snapshot:
                    self.step(target, "drop_snapshot", name=old.snapshot)
            except SeedError as error:
                log.warning("seed snapshot %s could not be dropped: %s", old.snapshot, error)
                continue
            self.store.drop(old.definition, old.key)

    def build_for(self, run_id: str, resource_id: str) -> SeedBuild:
        build = self._builds.get((run_id, resource_id))
        if build is None:
            raise SeedError(f"run {run_id} is not building a seed into backend resource {resource_id}; data ops run "
                            "in seed plans only")
        return build

    def building(self, run_id: str) -> bool:
        return any(key[0] == run_id for key in self._builds)

    def finish(self, run_id: str) -> list[dict[str, JsonValue]]:
        reports: list[dict[str, JsonValue]] = []
        try:
            for key in [key for key in self._builds if key[0] == run_id]:
                build = self._builds.pop(key)
                try:
                    manifest = self._snapshot(build)
                finally:
                    build.close()
                reports.append({"seed": manifest.seed, "key": manifest.key, "snapshot": manifest.snapshot,
                                "digest": manifest.digest, "outputsDigest": manifest.outputs_digest,
                                "resources": manifest.resources, "buildMs": manifest.build_ms,
                                "personas": list[JsonValue](sorted(manifest.personas)),
                                "deterministic": manifest.deterministic,
                                "divergence": manifest.divergence.model_dump(mode="json")
                                if manifest.divergence else None})
        finally:
            self._release(run_id)
        return reports

    def discard(self, run_id: str) -> None:
        for key in [key for key in self._builds if key[0] == run_id]:
            build = self._builds.pop(key)
            build.close()
            self._record_seed(build.target, None)
        self._release(run_id)

    def _release(self, run_id: str) -> None:
        for lock in self._held.pop(run_id, []):
            lock.release()


def _compare(previous: Manifest, current: Manifest) -> dict[str, Any]:
    paths = sorted(path for path in set(previous.outputs) | set(current.outputs)
                   if canonical_json(previous.outputs.get(path)) != canonical_json(current.outputs.get(path)))
    digest_differs = bool(previous.digest and current.digest and previous.digest != current.digest)
    if not paths and not digest_differs and previous.outputs_digest == current.outputs_digest:
        return {"deterministic": True, "divergence": None}
    return {"deterministic": False,
            "divergence": Divergence(paths=paths[:DIVERGENT_PATHS], digest=digest_differs, previous=previous.digest)}


__all__ = ["SeedEngine", "outputs_digest", "planned_seed", "snapshot_key"]
