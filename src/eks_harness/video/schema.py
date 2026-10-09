"""Master JSON Schema export.

Walks every registered IR root model and emits a single schema with ``$defs``
holding every variant of every discriminated union (effects, easings,
curves, media sources, marker sources, transitions, time refs). Plugin-
provided effect models that have been registered through
:func:`eks_harness.video.ir.register_effect_model` (typically via
:func:`eks_harness.video.plugins.ensure_registry`) are picked up automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from eks_harness.video.ir.curves import (
    LFO,
    BeatPulse,
    BoxEnv,
    Curve,
    Envelope,
    ExpDecay,
    ExpDecayEnv,
    Lambda,
    LinearEnv,
    Sine,
)
from eks_harness.video.ir.easing import Easing
from eks_harness.video.ir.effects import EffectAdapter, all_effect_models
from eks_harness.video.ir.markers import MarkerSource
from eks_harness.video.ir.media import MediaSource
from eks_harness.video.ir.project import Project
from eks_harness.video.ir.time import TimeRef
from eks_harness.video.ir.transitions import Transition

__all__ = ["export", "export_to_file"]


def _schema_for(value: Any) -> dict[str, Any]:
    adapter: TypeAdapter[Any] = TypeAdapter(value)
    return adapter.json_schema(schema_generator=GenerateJsonSchema, ref_template="#/$defs/{model}")


def export() -> dict[str, Any]:
    """Build the master schema dict.

    Each top-level discriminated union is exposed under ``$defs`` so plugins
    and Claude tooling can address them by name.
    """

    project_schema = Project.model_json_schema(ref_template="#/$defs/{model}")
    defs: dict[str, Any] = dict(project_schema.get("$defs", {}))

    for name, target in {
        "Easing": Easing,
        "Curve": Curve,
        "Envelope": Envelope,
        "MediaSource": MediaSource,
        "MarkerSource": MarkerSource,
        "Transition": Transition,
        "TimeRef": TimeRef,
    }.items():
        try:
            schema = _schema_for(target)
        except Exception:
            continue
        defs.update(schema.get("$defs", {}))
        defs[name] = {k: v for k, v in schema.items() if k != "$defs"}

    for model in (BeatPulse, ExpDecay, Sine, LFO, Lambda, ExpDecayEnv, LinearEnv, BoxEnv):
        try:
            sub = model.model_json_schema(ref_template="#/$defs/{model}")
        except Exception:
            continue
        defs.update(sub.get("$defs", {}))
        defs[model.__name__] = {k: v for k, v in sub.items() if k != "$defs"}

    _ = EffectAdapter

    effect_schema = _effect_schema()
    defs.update(effect_schema.get("$defs", {}))
    defs["Effect"] = {k: v for k, v in effect_schema.items() if k != "$defs"}

    project_schema["$defs"] = defs
    project_schema["title"] = "eks-harness video project"
    project_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return project_schema


def _effect_schema() -> dict[str, Any]:
    models = list(all_effect_models())
    one_of = [model.model_json_schema(ref_template="#/$defs/{model}") for model in models]
    defs: dict[str, Any] = {}
    cleaned: list[dict[str, Any]] = []
    for schema in one_of:
        defs.update(schema.get("$defs", {}))
        cleaned.append({k: v for k, v in schema.items() if k != "$defs"})
    return {
        "title": "Effect",
        "discriminator": {"propertyName": "kind"},
        "oneOf": cleaned,
        "$defs": defs,
    }


def export_to_file(path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(export(), indent=2, sort_keys=True), encoding="utf-8")
    return out
