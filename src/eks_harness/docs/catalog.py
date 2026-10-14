from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eks_harness.docs.markdown import (
    anchor,
    code,
    field_table,
    model_schema,
    page,
    sentence,
    table,
)
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    OVERLAY_RENDERERS,
    CatalogSpec,
    Category,
    Registry,
)

SECTIONS: tuple[tuple[Category, str, str], ...] = (
    ("effect", "Effects",
     ("Per-clip video effects of an edit, applied in order after the clip's source frame and before its transform. "
     "Attach them with `clip.effect(...)`; most have a factory on `eks.fx`.")),
    ("transition", "Transitions",
     ("Transitions between consecutive clips of a track, set with `clip.transition_in(...)` or "
     "`clip.transition_out(...)`; most have a factory on `eks.tx`.")),
    ("easing", "Easings",
     ("Easings for keyframes, animated values and transitions. Python takes the easing name as a string "
     "(`ease=\"out_cubic\"`) or a model from `eks.ease(...)`.")),
    ("curve", "Curves",
     ("Procedural curves for animatable fields: pass `eks.curves.<kind>(...)` wherever a field takes an animated "
     "value, next to plain numbers and `eks.keys(...)` keyframes.")),
    ("overlay", "Overlay kinds",
     ("Overlays share one model and three renderers: `live` draws in the app while a flow runs (`s.overlay.*`), "
     "`screenshot` draws on a captured image (`eks.annotate`, op `overlay.annotate`) and `video` draws on an edit "
     "track. Build them with `eks.overlay.<kind>(...)`.")),
    ("marker", "Marker sources",
     ("Analysis sources of `media.markers`: each turns a media file into time streams (beats, onsets, scene cuts, "
     "words) that symbolic times such as `beat:8` or `marker:intro` resolve against.")),
    ("target", "Target kinds",
     ("Predicates of the `Target` model, written with `by.<kind>(...)` or the string shorthand `<shorthand>=value` "
     "(`#id` is a test id, a bare string is exact text). Surfaces list where a kind resolves.")),
)
FACTORIES: dict[Category, str] = {
    "effect": "eks.fx",
    "transition": "eks.tx",
    "curve": "eks.curves",
    "overlay": "eks.overlay",
    "target": "by",
}
INTRO = """The catalogs the edit, overlay, analysis and target models draw from. Every entry is a pydantic model
with a `kind`; plugins add entries with `@eks.effect`, `@eks.transition`, `@eks.easing`, `@eks.curve`,
`@eks.overlay_kind`, `@eks.marker` and `@eks.target_kind`."""


def factory_resolver() -> Callable[[Category, str], str | None]:
    import eks

    namespaces: dict[Category, Any] = {
        "effect": eks.fx,
        "transition": eks.tx,
        "curve": eks.curves,
        "overlay": eks.overlay,
        "target": eks.by,
    }

    def resolve(category: Category, kind: str) -> str | None:
        namespace = namespaces.get(category)
        if namespace is None or not callable(getattr(namespace, kind, None)):
            return None
        return f"{FACTORIES[category]}.{kind}"

    return resolve


def builtin_entries(registry: Registry, category: Category) -> list[CatalogSpec]:
    return sorted((spec for spec in registry.catalogs(category) if spec.provenance.plugin == BUILTIN_PLUGIN),
                  key=lambda spec: spec.kind)


def entry_rows(spec: CatalogSpec, python: str | None) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = [("Model", code(spec.model.__name__))]
    if python:
        rows.append(("Python", code(python)))
    if spec.shorthand:
        rows.append(("Shorthand", code(f"{spec.shorthand}=")))
    if spec.surfaces:
        rows.append(("Surfaces", ", ".join(sorted(spec.surfaces))))
    if spec.category == "overlay":
        rows.append(("Renderers", ", ".join(name for name in OVERLAY_RENDERERS if name in spec.renderers) or "none"))
    if spec.platforms is not None:
        rows.append(("Platforms", ", ".join(sorted(spec.platforms))))
    if spec.device_dependent:
        rows.append(("Device dependent", "yes"))
    return rows


def render_entry(spec: CatalogSpec, python: str | None) -> str:
    parts = [f"### {spec.category}: {spec.kind}"]
    if spec.description:
        parts.append(sentence(spec.description))
    parts.append(table(["Property", "Value"], entry_rows(spec, python)))
    parts.append(field_table(model_schema(spec.model), name="Field", empty="No fields."))
    return "\n\n".join(parts)


def render_catalog(registry: Registry) -> str:
    resolve = factory_resolver()
    index_rows = []
    blocks: list[str] = []
    for category, title, intro in SECTIONS:
        entries = builtin_entries(registry, category)
        index_rows.append((f"[{title}](#{anchor(title)})", str(len(entries)),
                           ", ".join(code(spec.kind) for spec in entries)))
        blocks.extend([f"## {title}", intro])
        blocks.extend(render_entry(spec, resolve(category, spec.kind)) for spec in entries)
    return page("Catalog reference", INTRO, table(["Catalog", "Entries", "Kinds"], index_rows), *blocks)


__all__ = ["builtin_entries", "render_catalog", "render_entry"]
