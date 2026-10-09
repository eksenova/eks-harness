"""Enumerate effects + transitions for the catalog grid.

Builds a list of :class:`CatalogEntry` records - one per effect class
returned by :func:`eks_harness.video.ir.effects.all_effect_models`, plus the
hard-coded transition set ``Cut`` / ``Crossfade`` / ``PluginTransition``.
Each entry carries the JSON schema, a stable ``preview_url`` (an mp4
URL, only populated when a cached artifact already exists on disk),
and a ``stale`` flag derived from comparing the sidecar key to a
freshly-computed one.
"""

from __future__ import annotations

from typing import Any

from eks_harness.video.ir.effects import all_effect_models
from eks_harness.video.ir.transitions import (
    BeatFlash,
    BeatGlitch,
    BlurThrough,
    Crossfade,
    Cut,
    DipToBlack,
    DipToWhite,
    Dissolve,
    FadeGrays,
    FilmBurn,
    GlitchTransition,
    Iris,
    LightLeak,
    LumaWipe,
    Pixelize,
    PluginTransition,
    Push,
    Radial,
    RGBShiftWipe,
    Slide,
    WhipPan,
    Wipe,
    ZoomTransition,
)

from .preview_cache import (
    _compute_effect_key,
    _compute_transition_key,
    is_fresh,
    preview_url,
)
from .preview_renderer import default_params_for_effect, non_previewable_reason

__all__ = ["list_effects", "list_transitions"]


def _build_effect_entry(cls: type) -> dict[str, Any]:
    name = cls.__name__
    previewable_reason: str | None = None
    try:
        defaults = default_params_for_effect(name)
        default_dump: Any = {
            k: (v if not hasattr(v, "model_dump") else v.model_dump(mode="json"))
            for k, v in defaults.items()
        }
        previewable = True
    except Exception as exc:
        default_dump = None
        previewable = False
        # Prefer the curated reason map; fall back to the exception string so
        # diagnostic info still surfaces for unknown failure modes.
        previewable_reason = non_previewable_reason(name) or str(exc) or None

    # Non-previewable effects never enter the cache, so skip the key
    # computation entirely - ``_compute_effect_key`` now raises
    # ``PreviewUnavailableError`` for those, which we'd just have to
    # swallow anyway.
    if previewable:
        try:
            key = _compute_effect_key(name)
        except Exception:
            key = None
            fresh = False
        else:
            fresh = is_fresh(name, "effects", key)
    else:
        key = None
        fresh = False
    url = preview_url(name, "effects") if fresh else None
    return {
        "name": name,
        "kind": "effect",
        "description": (cls.__doc__ or "").strip().splitlines()[0]
        if cls.__doc__
        else None,
        "schema": cls.model_json_schema(),
        "default_params": default_dump,
        "preview_url": url,
        "key": key if fresh else None,
        "stale": not fresh,
        "previewable": previewable,
        "previewable_reason": previewable_reason,
    }


def list_effects() -> list[dict[str, Any]]:
    """Return one catalog dict per registered effect class.

    Returns plain dicts (not :class:`CatalogEntry` pydantic models) so the
    caller can wrap them inside its own reply envelope without paying a
    double-serialise cost.
    """

    return [_build_effect_entry(cls) for cls in all_effect_models()]


def _build_transition_entry(name: str, cls: type) -> dict[str, Any]:
    key = _compute_transition_key(name, None)
    fresh = is_fresh(name, "transitions", key)
    url = preview_url(name, "transitions") if fresh else None
    return {
        "name": name,
        "kind": "transition",
        "description": (cls.__doc__ or "").strip().splitlines()[0]
        if cls.__doc__
        else None,
        "schema": cls.model_json_schema(),
        "default_params": None,
        "preview_url": url,
        "key": key if fresh else None,
        "stale": not fresh,
        "previewable": True,
        "previewable_reason": None,
    }


def list_transitions() -> list[dict[str, Any]]:
    """Return catalog dicts for every built-in transition kind."""

    return [
        _build_transition_entry("Cut", Cut),
        _build_transition_entry("Crossfade", Crossfade),
        _build_transition_entry("DipToBlack", DipToBlack),
        _build_transition_entry("DipToWhite", DipToWhite),
        _build_transition_entry("Wipe", Wipe),
        _build_transition_entry("Slide", Slide),
        _build_transition_entry("Push", Push),
        _build_transition_entry("GlitchTransition", GlitchTransition),
        _build_transition_entry("ZoomTransition", ZoomTransition),
        _build_transition_entry("Dissolve", Dissolve),
        _build_transition_entry("BlurThrough", BlurThrough),
        _build_transition_entry("Iris", Iris),
        _build_transition_entry("Radial", Radial),
        _build_transition_entry("Pixelize", Pixelize),
        _build_transition_entry("FadeGrays", FadeGrays),
        _build_transition_entry("LumaWipe", LumaWipe),
        _build_transition_entry("RGBShiftWipe", RGBShiftWipe),
        _build_transition_entry("WhipPan", WhipPan),
        _build_transition_entry("LightLeak", LightLeak),
        _build_transition_entry("FilmBurn", FilmBurn),
        _build_transition_entry("BeatFlash", BeatFlash),
        _build_transition_entry("BeatGlitch", BeatGlitch),
        _build_transition_entry("PluginTransition", PluginTransition),
    ]
