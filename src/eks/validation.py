from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from eks.discovery import DiscoveryError, find_app, legacy_backends
from eks.workspace import seed_info, workspace_registry
from eks_harness.engine.validator import Report, SeedInfo, Validator
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry, default_registry


class _Apps:
    def __init__(self, tree: Path) -> None:
        self._tree = tree
        self._found: dict[str, Sequence[str] | None] = {}

    def __call__(self, name: str) -> Sequence[str] | None:
        if name not in self._found:
            try:
                info = find_app(self._tree, name)
            except DiscoveryError as error:
                raise LookupError(str(error)) from error
            self._found[name] = None if info is None else info.platforms
        return self._found[name]


def validate(plan: Plan, *, registry: Registry | None = None, tree: Path | None = None) -> Report:
    if registry is not None:
        chosen = registry
    elif tree is not None:
        chosen = workspace_registry(tree)
    else:
        chosen = default_registry()

    def seeds(name: str) -> SeedInfo | None:
        return seed_info(name, registry=chosen)

    apps = _Apps(tree) if tree is not None else None
    backends = known_backends(chosen, tree) if tree is not None else None
    return Validator(chosen, seeds=seeds, apps=apps, versions=chosen.versions(), backends=backends).validate(plan)


def known_backends(registry: Registry, tree: Path) -> frozenset[str]:
    return frozenset({spec.name for spec in registry.backends()} | set(legacy_backends(tree)))


__all__ = ["Report", "known_backends", "validate"]
