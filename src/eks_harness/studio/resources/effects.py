"""``eks-harness://video/effects/{name}`` - schema + docs for one effect."""

from __future__ import annotations

import json
from typing import Any

from ._spec import ResourceSpec

__all__ = ["RESOURCES", "read_effect"]


def read_effect(name: str) -> str:
    return json.dumps(_describe_effect(name), indent=2, sort_keys=True)


def _describe_effect(name: str) -> dict[str, Any]:
    try:
        from eks_harness.video.ir.effects import all_effect_models
    except Exception as exc:
        return {"name": name, "error": f"effect catalog unavailable: {exc}"}

    for model in all_effect_models():
        kind = _kind_of(model)
        if kind == name or model.__name__ == name:
            schema = model.model_json_schema(ref_template="#/$defs/{model}")
            return {
                "name": kind or model.__name__,
                "class": model.__name__,
                "doc": (model.__doc__ or "").strip(),
                "compile_targets": sorted(getattr(model, "compile_targets", set()) or []),
                "schema": schema,
            }

    raise KeyError(f"unknown effect {name!r}")


def _kind_of(model: type) -> str | None:
    try:
        field = model.model_fields.get("kind")  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if field is None:
        return None
    return getattr(field, "default", None)


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/effects/{name}', name='video_effect_schema', title='Effect schema',
                 description='JSON schema and docstring of a registered effect IR model.',
                 mime_type='application/json', read=read_effect, needs_roots=False, needs_registry=False),
]
