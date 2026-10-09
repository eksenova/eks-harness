"""``eks-harness://video/plugins/{name}`` - describe a loaded plugin from the registry."""

from __future__ import annotations

import json
from typing import Any

from ._spec import ResourceSpec

__all__ = ["RESOURCES", "read_plugin"]


def read_plugin(name: str) -> str:
    return json.dumps(_describe_plugin(name), indent=2, sort_keys=True)


def _describe_plugin(name: str) -> dict[str, Any]:
    try:
        from eks_harness.video.plugins.registry import ensure_registry, registry_snapshot
    except Exception as exc:
        return {"name": name, "error": f"registry unavailable: {exc}"}

    ensure_registry()
    snapshot = registry_snapshot()
    found_in: list[str] = [category for category, names in snapshot.items() if name in names]
    if not found_in:
        raise KeyError(f"no plugin named {name!r} is registered")

    return {
        "name": name,
        "categories": sorted(found_in),
        "registry_snapshot": snapshot,
    }


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/plugins/{name}', name='video_plugin', title='Video plugin',
                 description='A loaded video plugin and the categories it registers in.',
                 mime_type='application/json', read=read_plugin, needs_roots=False, needs_registry=False),
]
