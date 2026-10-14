from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from eks.data import CREATE_OP, EXTEND_OP
from eks.discovery import (
    _RELEASE_HOOKS,
    find_tree,
    load_plugins,
    load_tree,
    resolve_entry,
    split_spec,
    sync_tree,
    tree_registry,
)
from eks.planning import (
    coerce_params,
    seed_plan,
    trace,
)
from eks_harness.engine.validator import SeedInfo
from eks_harness.ir.entries import SeedOptions
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import (
    EntrySpec,
    Registry,
    ResourceSpec,
    default_registry,
    using_registry,
)

PRELOADED_KINDS = ("seed", "scene")


@dataclass(frozen=True, slots=True)
class _SeedPersonas:
    entry: EntrySpec
    resources: tuple[ResourceSpec, ...]
    personas: tuple[str, ...]
    extends: tuple[tuple[str, str | None], ...]
    plan: Plan

    @property
    def bases(self) -> tuple[str, ...]:
        return tuple(base for base, _hash in self.extends)

    def matches(self, entry: EntrySpec, resources: tuple[ResourceSpec, ...]) -> bool:
        return (self.entry is entry and len(self.resources) == len(resources)
                and all(cached is current for cached, current in zip(self.resources, resources, strict=True)))


_SEEDS: dict[tuple[int, str], _SeedPersonas] = {}
_SEED_REGISTRIES: OrderedDict[int, None] = OrderedDict()
_SEED_CAPACITY = 64
_LOCK = threading.RLock()
_RELEASED: set[int] = set()
_RELEASED_LOCK = threading.Lock()


def _drop_seeds(registry_ids: set[int]) -> None:
    for key in [key for key in _SEEDS if key[0] in registry_ids]:
        del _SEEDS[key]
    for registry_id in registry_ids:
        _SEED_REGISTRIES.pop(registry_id, None)


def _apply_released() -> None:
    with _RELEASED_LOCK:
        released = set(_RELEASED)
        _RELEASED.clear()
    if released:
        _drop_seeds(released)


def _forget_registries(registries: Sequence[Registry]) -> None:
    with _RELEASED_LOCK:
        _RELEASED.update(id(registry) for registry in registries)
    if _LOCK.acquire(blocking=False):
        try:
            _apply_released()
        finally:
            _LOCK.release()


def _remember(key: tuple[int, str], value: _SeedPersonas) -> None:
    registry_id = key[0]
    _SEEDS[key] = value
    _SEED_REGISTRIES[registry_id] = None
    _SEED_REGISTRIES.move_to_end(registry_id)
    while len(_SEED_REGISTRIES) > _SEED_CAPACITY:
        _drop_seeds({next(iter(_SEED_REGISTRIES))})


_RELEASE_HOOKS.append(_forget_registries)


def _entry_file(spec: str, cwd: Path | None) -> Path:
    path, _name = split_spec(spec)
    path = path.expanduser()
    return path if path.is_absolute() else (cwd or Path.cwd()) / path


def plan_spec(
    spec: str,
    *,
    params: Mapping[str, str] | None = None,
    platform: str | None = None,
    cwd: Path | None = None,
    plugins: bool = True,
    registry: Registry | None = None,
) -> Plan:
    tree = find_tree(_entry_file(spec, cwd).parent)
    chosen = registry if registry is not None else workspace_registry(tree, plugins=plugins)
    with using_registry(chosen):
        entry = resolve_entry(spec, cwd=cwd)
        values = coerce_params(entry, dict(params or {}), text=True)
        return trace(entry, params=values, platform=platform, tree=tree, registry=chosen)


def workspace_registry(tree: Path | None, *, plugins: bool = True, isolated: bool = False) -> Registry:
    if isolated:
        registry = tree_registry(tree, plugins=plugins, isolated=True)
        with using_registry(registry):
            if plugins:
                load_plugins(tree, prune=True)
            if tree is not None:
                sync_tree(tree, kinds=PRELOADED_KINDS)
        return registry
    registry = tree_registry(tree, plugins=plugins) if tree is not None else default_registry()
    with using_registry(registry):
        if plugins:
            load_plugins(tree)
        if tree is not None:
            load_tree(tree, kinds=PRELOADED_KINDS)
    return registry


def _own_personas(name: str, chosen: Registry, visiting: frozenset[str] = frozenset()) -> _SeedPersonas | None:
    entry = next((spec for spec in chosen.entries("seed") if spec.name == name), None)
    if entry is None:
        return None
    resources = tuple(chosen.resources())
    key = (id(chosen), name)
    inside = visiting | {name}
    with _LOCK:
        _apply_released()
        cached = _SEEDS.get(key)
        if cached is not None and cached.matches(entry, resources) and _bases_current(cached, chosen, inside):
            return cached
        plan = seed_plan(entry, registry=chosen)
        extends = tuple(dict.fromkeys(
            (base, recorded if isinstance(recorded := node.args.get("hash"), str) else None)
            for node in plan.nodes
            if node.op == EXTEND_OP and isinstance(base := node.args.get("base"), str)
        ))
        result = _SeedPersonas(entry, resources, tuple(plan.personas), extends, plan)
        _remember(key, result)
        return result


def _bases_current(cached: _SeedPersonas, chosen: Registry, visiting: frozenset[str]) -> bool:
    for base, recorded in cached.extends:
        if base in visiting:
            if recorded is not None:
                return False
            continue
        current = _own_personas(base, chosen, visiting)
        if (current.plan.hash if current is not None else None) != recorded:
            return False
    return True


def _collect_personas(name: str, chosen: Registry, seen: set[str], into: dict[str, None]) -> bool:
    if name in seen:
        return True
    seen.add(name)
    own = _own_personas(name, chosen)
    if own is None:
        return False
    for base in own.bases:
        _collect_personas(base, chosen, seen, into)
    into.update(dict.fromkeys(own.personas))
    return True


def seed_info(name: str, *, registry: Registry | None = None) -> SeedInfo | None:
    chosen = registry if registry is not None else default_registry()
    own = _own_personas(name, chosen)
    if own is None:
        return None
    personas = seed_personas(name, registry=chosen) or []
    options = own.entry.options
    backend = options.backend if isinstance(options, SeedOptions) else ""
    resources: dict[str, str] = {}
    _collect_resources(name, chosen, set(), resources)
    return SeedInfo(name=name, backend=backend, now=own.plan.clock.now, hash=own.plan.hash,
                    personas=tuple(personas), bases=own.bases, resources=tuple(resources.items()))


def _collect_resources(name: str, chosen: Registry, seen: set[str], into: dict[str, str]) -> None:
    if name in seen:
        return
    seen.add(name)
    own = _own_personas(name, chosen)
    if own is None:
        return
    for base in own.bases:
        _collect_resources(base, chosen, seen, into)
    for node, _ancestors in own.plan.walk():
        kind, path = node.args.get("kind"), node.args.get("path")
        if node.op == CREATE_OP and isinstance(kind, str) and isinstance(path, str):
            into.setdefault(path, kind)


def seed_personas(name: str, *, registry: Registry | None = None) -> list[str] | None:
    chosen = registry if registry is not None else default_registry()
    personas: dict[str, None] = {}
    if not _collect_personas(name, chosen, set(), personas):
        return None
    return list(personas)


def planned_seed(name: str, *, registry: Registry | None = None) -> Plan | None:
    chosen = registry if registry is not None else default_registry()
    own = _own_personas(name, chosen)
    return own.plan if own is not None else None


__all__ = ["plan_spec", "planned_seed", "seed_info", "seed_personas", "workspace_registry"]
