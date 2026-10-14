from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue, ValidationError

from eks_harness.data.context import ResourceContext, SeedApi, SeedError
from eks_harness.data.store import Manifest, ReplayEntry
from eks_harness.engine.errors import RefResolutionError
from eks_harness.engine.model import BackendHandle, RunInfo
from eks_harness.engine.values import ROOT_INSTANCE, ValueStore, resolve
from eks_harness.ir.hashing import canonical_json, derived_seed, digest
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import CREATE_HOOK, Registry, RegistryError
from eks_harness.plugins.sdk import BackendInfo

if TYPE_CHECKING:
    from eks_harness.data.engine import SeedEngine

PHASES: tuple[str, ...] = ("schema", "api", "finalize", "mongo")
REPLAY_PHASE = "mongo"
CREATE_OP = "data.create"
EXTEND_OP = "data.extend"
BASE_OP = "data.base"
SEED_OPS = frozenset({CREATE_OP, EXTEND_OP, BASE_OP})
DEFAULT_NOW = "2026-01-01T00:00:00+00:00"


def phase_index(phase: str) -> int:
    try:
        return PHASES.index(phase)
    except ValueError:
        raise SeedError(f"unknown seed phase {phase!r}; phases are {', '.join(PHASES)}") from None


def parse_now(value: str | None) -> datetime:
    parsed = datetime.fromisoformat(value or DEFAULT_NOW)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _object(value: object) -> dict[str, JsonValue]:
    return {str(name): item for name, item in value.items()} if isinstance(value, dict) else {}


def outputs_digest(outputs: Mapping[str, JsonValue]) -> str:
    return digest(canonical_json(dict(outputs)))


@dataclass
class BuildTarget:
    run: RunInfo
    handle: BackendHandle
    registry: Registry
    tree: Path | None

    @property
    def backend_id(self) -> str:
        return self.handle.identity.backend_id

    @property
    def definition(self) -> str:
        return self.handle.identity.definition

    @property
    def info(self) -> BackendInfo:
        return BackendInfo.from_dict(self.handle.secrets)


@dataclass
class BuildState:
    outputs: dict[str, JsonValue] = field(default_factory=dict)
    personas: dict[str, JsonValue] = field(default_factory=dict)
    replay: list[ReplayEntry] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)


class SeedBuild:
    def __init__(self, engine: SeedEngine, target: BuildTarget, *, seed: str, plan: Plan, key: str,
                 migrations: str | None, log: Callable[[str], None]) -> None:
        self.engine = engine
        self.target = target
        self.seed = seed
        self.plan = plan
        self.key = key
        self.migrations = migrations
        self.log = log
        self.values = ValueStore()
        self.state = BuildState()
        self.base: Manifest | None = None
        self.entered: set[str] = set()
        self.created = 0
        self.started = time.monotonic()
        self.now = parse_now(plan.clock.now)
        self.root_seed = plan.clock.seed or 0
        self._lock = threading.RLock()
        self._api: SeedApi | None = None

    @property
    def extends(self) -> list[Node]:
        return [node for node in self.plan.nodes if node.op == EXTEND_OP]

    def begin(self) -> None:
        if not self.extends:
            self.engine.step(self.target, "reset", required=True)

    def api(self) -> SeedApi | None:
        if self._api is None and self.target.handle.url:
            self._api = SeedApi(self.target.handle.url, auth=self._auth)
        return self._api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None

    def _auth(self, persona: str) -> Mapping[str, str]:
        return self.engine.persona_headers(self.target, self.state.personas, persona)

    def enter_phase(self, phase: str) -> None:
        with self._lock:
            if phase in self.entered:
                return
            self.entered.add(phase)
        self.log(f"phase {phase}")
        self.engine.step(self.target, "seed_phase", phase=phase)

    def _node(self, node_id: str) -> Node:
        found = next((node for node in self.plan.nodes if node.id == node_id), None)
        if found is None:
            raise SeedError(f"node {node_id} is not part of seed {self.seed}")
        return found

    def _resolved(self, node: Node) -> dict[str, Any]:
        try:
            data = resolve(json.loads(json.dumps(node.args)), self.values, ROOT_INSTANCE)
        except RefResolutionError as error:
            raise SeedError(f"{node.op} {node.id}: {error.message}") from error
        if not isinstance(data, dict):
            raise SeedError(f"{node.op} {node.id}: arguments are not an object")
        return data

    def extend(self, node: Node) -> JsonValue:
        base = str(node.args.get("base") or "")
        with self._lock:
            if self.base is not None and self.base.seed != base:
                raise SeedError(f"seed {self.seed} extends {self.base.seed} and {base}; a seed can extend one base, "
                                "since restoring a snapshot replaces the backend's data")
            if self.base is None:
                self.base = self.engine.ensure_base(self.target, base, log=self.log)
                self.state.outputs.update(self.base.outputs)
                self.state.personas.update(self.base.personas)
                self.state.replay.extend(self.base.replay)
                self.state.bases = [*self.base.bases, self.base.key]
        value: JsonValue = {"base": base, "key": self.base.key, "resources": self.base.resources}
        self.values.put(node.id, ROOT_INSTANCE, value)
        return value

    def base_resource(self, node: Node) -> JsonValue:
        path = str(node.args.get("path") or "")
        if path not in self.state.outputs:
            raise SeedError(f"the base of seed {self.seed} created no resource at {path}")
        value = self.state.outputs[path]
        self.values.put(node.id, ROOT_INSTANCE, value)
        return value

    def create(self, node: Node, log: Callable[[str], None] | None = None) -> JsonValue:
        args = node.args
        kind, path = str(args.get("kind")), str(args.get("path"))
        phase = str(args.get("phase") or "api")
        phase_index(phase)
        resolved = self._resolved(node)
        self.enter_phase(phase)
        raw_fields = _object(args.get("fields"))
        fields = _object(resolved.get("fields"))
        key = args.get("key")
        key = key if isinstance(key, str) else None
        output = self.run_create(kind=kind, key=key, path=path, phase=phase, raw_fields=raw_fields, fields=fields,
                                 parent=resolved.get("parent"), log=log)
        with self._lock:
            self.values.put(node.id, ROOT_INSTANCE, output)
            self.state.outputs[path] = output
            self.created += 1
            if phase == REPLAY_PHASE:
                self.state.replay.append(ReplayEntry(node=node.id, path=path, kind=kind, key=key, phase=phase,
                                                     fields=raw_fields, resolved=fields,
                                                     parent=resolved.get("parent")))
            if self.engine.is_persona(self.target.registry, kind) and key:
                self.state.personas[key] = output
        return output

    def run_create(self, *, kind: str, key: Any, path: str, phase: str, raw_fields: Mapping[str, Any],
                   fields: Mapping[str, Any], parent: Any, log: Callable[[str], None] | None = None) -> JsonValue:
        note = log or self.log
        try:
            spec = self.target.registry.resource(kind)
        except RegistryError as error:
            raise SeedError(f"{path}: {error}") from error
        hook = getattr(spec.model, CREATE_HOOK, None)
        if hook is None:
            raise SeedError(f"{path}: resource kind {kind} has no create(self, ctx) method, so the seed engine cannot "
                            "create it")
        try:
            model = spec.model.model_validate(dict(raw_fields))
        except ValidationError as error:
            raise SeedError(f"{path}: the fields do not validate against {spec.model.__name__}: "
                            f"{error.errors()[0].get('msg')}") from error
        ctx = ResourceContext(kind=kind, key=key, path=path, phase=phase, parent=parent, fields=fields,
                              raw_fields=raw_fields, api=self.api(), backend=self.target.info, now=self.now,
                              seed=derived_seed(self.root_seed, path), log=note)
        started = time.monotonic()
        try:
            result = hook(model, ctx)
        except SeedError as error:
            raise SeedError(f"{path} ({kind}): {error.message}", detail=error.detail) from error
        except Exception as error:
            raise SeedError(f"{path} ({kind}): {type(error).__name__}: {error}") from error
        output = {} if result is None else result
        if not isinstance(output, dict):
            raise SeedError(f"{path} ({kind}): create returned {type(output).__name__}; it must return a JSON object "
                            "or None")
        try:
            plain = json.loads(json.dumps(output))
        except (TypeError, ValueError) as error:
            raise SeedError(f"{path} ({kind}): create returned a value that is not JSON: {error}") from error
        note(f"{path} created in {(time.monotonic() - started) * 1000:.0f} ms")
        return plain

    def order(self) -> list[Node]:
        others = [node for node in self.plan.nodes if node.op != EXTEND_OP]
        for node in others:
            if node.op not in SEED_OPS:
                raise SeedError(f"seed {self.seed} has a {node.op} node ({node.id}); seeds are built from data ops "
                                "only")
        ranked = sorted(enumerate(others), key=lambda item: (
            phase_index(str(item[1].args.get("phase") or "api")) if item[1].op == CREATE_OP else -1, item[0]))
        return [*self.extends, *(node for _, node in ranked)]

    def run_plan(self) -> None:
        self.begin()
        for node in self.order():
            if node.op == EXTEND_OP:
                self.extend(node)
            elif node.op == BASE_OP:
                self.base_resource(node)
            else:
                self.create(node)

    def replay(self, entries: Sequence[ReplayEntry], expected: Mapping[str, JsonValue]) -> list[str]:
        if entries:
            self.enter_phase(REPLAY_PHASE)
        diverged: list[str] = []
        for entry in entries:
            output = self.run_create(kind=entry.kind, key=entry.key, path=entry.path, phase=entry.phase,
                                     raw_fields=entry.fields, fields=entry.resolved, parent=entry.parent)
            if entry.path in expected and canonical_json(expected[entry.path]) != canonical_json(output):
                diverged.append(entry.path)
        return diverged

    def manifest(self, *, snapshot: str | None, digest_value: str | None) -> Manifest:
        return Manifest(key=self.key, seed=self.seed, seedHash=self.plan.hash or "", definition=self.target.definition,
                        migrations=self.migrations, snapshot=snapshot, digest=digest_value,
                        outputsDigest=outputs_digest(self.state.outputs), outputs=dict(self.state.outputs),
                        personas=dict(self.state.personas), replay=list(self.state.replay),
                        bases=list(self.state.bases), created=datetime.now(UTC).isoformat(timespec="seconds"),
                        buildMs=int((time.monotonic() - self.started) * 1000), resources=len(self.state.outputs),
                        runs=[self.target.run.id])


__all__ = ["PHASES", "SEED_OPS", "BuildTarget", "SeedBuild", "outputs_digest", "parse_now", "phase_index"]
