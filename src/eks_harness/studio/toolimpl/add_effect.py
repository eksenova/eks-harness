"""``add_effect`` tool.

Appends a serialized effect to ``segment.effects`` (in ``project.json``)
on the segment identified by ``segment_id``. The effect dict is validated
through :data:`eks_harness.video.ir.EffectAdapter` so plugin-registered effects are
honoured. The full project is re-validated before the snapshot is rewritten.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from eks_harness.video.ir import EffectAdapter, Project

from ..roots import RootsManager
from ._project_io import load_project_json, resolve_project_path

__all__ = ["DESCRIPTION", "run"]

_LOG = logging.getLogger(__name__)


def _run(
    *,
    project_path: Path,
    segment_id: str,
    effect_json: dict[str, Any],
    roots: RootsManager,
) -> dict[str, Any]:
    resolved = resolve_project_path(project_path, roots)
    json_path, raw = load_project_json(resolved)

    effect = EffectAdapter.validate_python(effect_json)
    effect_dump = _dump_effect(effect)

    target_segment, track_name = _find_segment(raw, segment_id)
    if target_segment is None:
        raise ValueError(f"segment_id {segment_id!r} not found in any track")

    effects = target_segment.setdefault("effects", [])
    if not isinstance(effects, list):
        raise ValueError(f"segment {segment_id!r} effects field is not a list")
    effects.append(effect_dump)

    try:
        validated = Project.model_validate(raw, by_alias=True)
    except ValidationError as exc:
        _LOG.warning("add_effect rejected by Project validator: %s", exc.errors())
        raise

    json_path.write_text(
        validated.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "updated_path": str(json_path),
        "segment_id": segment_id,
        "track_name": track_name,
        "effect_kind": effect_dump.get("kind"),
        "effect_index": len(effects) - 1,
    }


def _find_segment(
    raw: dict[str, Any], segment_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    tracks = raw.get("tracks", []) or []
    if not isinstance(tracks, list):
        return None, None
    for track in tracks:
        if not isinstance(track, dict):
            continue
        for segment in track.get("segments", []) or []:
            if isinstance(segment, dict) and segment.get("id") == segment_id:
                return segment, track.get("name")
    return None, None


def _dump_effect(effect: Any) -> dict[str, Any]:
    """Serialize a validated effect back to a JSON dict.

    ``EffectAdapter.validate_python`` returns the concrete model instance;
    we round-trip through ``model_dump`` to keep defaults explicit and the
    snapshot stable.
    """

    return effect.model_dump(by_alias=True)

DESCRIPTION = 'Append an effect (as a JSON dict with discriminator kind) to the segment with the given segment_id in project.json. Validated through EffectAdapter so plugin effects are honoured.'
run = _run
