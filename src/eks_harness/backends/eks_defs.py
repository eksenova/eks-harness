from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

EKS_SCHEME = "eks:"

log = logging.getLogger(__name__)


def is_eks_path(path: str | Path) -> bool:
    return str(path).startswith(EKS_SCHEME)


def eks_path(name: str) -> str:
    return f"{EKS_SCHEME}{name}"


def eks_runner_command(path: str | Path) -> list[str]:
    name = str(path)[len(EKS_SCHEME):]
    return [sys.executable, "-m", "eks_harness.backends.runner", "eks", name]


def _registry(tree: str | Path | None) -> Any:
    from eks.workspace import workspace_registry

    return workspace_registry(Path(tree) if tree else None)


def eks_definitions(tree: str | None) -> list[dict[str, Any]]:
    if not tree:
        return []
    try:
        registry = _registry(tree)
        specs = registry.backends()
    except (ImportError, OSError, SyntaxError, ValueError, RuntimeError) as error:
        log.warning("the eks backends of %s could not be loaded: %s", tree, error)
        return []
    found: list[dict[str, Any]] = []
    for spec in specs:
        item: dict[str, Any] = {
            "name": spec.name, "path": eks_path(spec.name), "source": "eks", "plugin": spec.provenance.plugin,
            "tree": tree, "description": spec.description, "identity": spec.identity,
            "requires": [requirement.as_dict() for requirement in spec.requires]}
        if spec.idle_timeout is not None:
            item["idleTimeout"] = spec.idle_timeout
        found.append(item)
    return found


def load_eks_backend(tree: str | None, name: str) -> Any:
    from eks_harness.ir.registry import implementation

    registry = _registry(tree)
    spec = registry.backend(name)
    impl = implementation(spec.impl)
    if impl is None:
        raise RuntimeError(f"backend {name}: {spec.impl} cannot be loaded from {tree or 'the default registry'}")
    return impl() if isinstance(impl, type) else impl


__all__ = ["EKS_SCHEME", "eks_definitions", "eks_path", "eks_runner_command", "is_eks_path", "load_eks_backend"]
