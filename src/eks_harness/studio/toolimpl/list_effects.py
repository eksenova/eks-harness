"""``list_effects`` tool.

Enumerates every effect currently registered in the IR (built-ins plus any
plugin-installed effects). The output is what an LLM client needs to
discover the available effect vocabulary at runtime - kind discriminator,
compile targets, docstring, and field definitions.
"""

from __future__ import annotations

import logging
from typing import Any

from eks_harness.video.ir.effects import EffectIR, all_effect_models

__all__ = ["DESCRIPTION", "run"]

_LOG = logging.getLogger(__name__)


def _run() -> dict[str, Any]:
    effects: list[dict[str, Any]] = []
    for model in all_effect_models():
        effects.append(_describe_effect_model(model))
    return {"effects": effects}


def _describe_effect_model(model: type[EffectIR]) -> dict[str, Any]:
    kind_field = model.model_fields.get("kind")
    kind_value: str | None = None
    if kind_field is not None and kind_field.default is not None:
        kind_value = kind_field.default
    return {
        "name": model.__name__,
        "kind": kind_value,
        "compile_targets": sorted(model.compile_targets),
        "doc": (model.__doc__ or "").strip(),
        "fields": _describe_fields(model),
    }


def _describe_fields(model: type[EffectIR]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for name, info in model.model_fields.items():
        if name == "kind":
            continue
        fields.append(
            {
                "name": name,
                "type": _render_annotation(info.annotation),
                "required": info.is_required(),
                "default": _render_default(info),
                "description": info.description,
                "alias": info.alias,
            }
        )
    return fields


def _render_annotation(annotation: Any) -> str:
    if annotation is None:
        return "None"
    return _stringify(annotation)


def _stringify(value: Any) -> str:
    module = getattr(value, "__module__", "")
    qualname = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if qualname and module and not module.startswith(("builtins", "typing")):
        return f"{module}.{qualname}"
    return repr(value)


def _render_default(info: Any) -> Any:
    if info.is_required():
        return None
    default = info.default
    factory = info.default_factory
    if factory is not None:
        try:
            produced = factory()
        except Exception:
            return f"<factory {factory!r}>"
        return _safe_serialize(produced)
    return _safe_serialize(default)


def _safe_serialize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_serialize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_serialize(v) for k, v in value.items()}
    try:
        from pydantic import BaseModel

        if isinstance(value, BaseModel):
            return value.model_dump(by_alias=True)
    except ImportError:
        pass
    return repr(value)

DESCRIPTION = 'List every registered video effect IR model with its kind, compile_targets, docstring, and field schema. Reflects plugin registrations as well as built-ins.'
run = _run
