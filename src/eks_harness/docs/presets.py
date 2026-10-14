from __future__ import annotations

from eks_harness.docs.markdown import (
    anchor,
    code,
    json_block,
    model_field_table,
    page,
    sentence,
    table,
)
from eks_harness.ir.registry import BUILTIN_PLUGIN, PresetSpec, Registry

USES = {
    "network": "Named by `s.net.shape(profile)` (op `net.shape`).",
    "pace": "Named by `pace=` of `s.record(...)` and of the session; it sets how recordings move, dwell and type.",
    "style": "Named by `style=` of an overlay; it sets colours, fonts and sizes of the overlay renderers.",
    "viewport": "Named by `viewport=` of a flow, a checkpoint, a shot or a recording. A viewport of the app profile "
                "with the same name wins.",
}
INTRO = """Named definitions that plans refer to by name. When a plan is built the name stays in the plan; the
engine resolves it to the value below when it runs the op. Plugins and projects add presets with
`@eks.preset(name, category=...)`."""


def builtin_presets(registry: Registry) -> dict[str, list[PresetSpec]]:
    grouped: dict[str, list[PresetSpec]] = {}
    for spec in sorted(registry.presets(), key=lambda item: (item.category, item.name)):
        if spec.provenance.plugin == BUILTIN_PLUGIN:
            grouped.setdefault(spec.category, []).append(spec)
    return grouped


def render_preset(spec: PresetSpec) -> str:
    parts = [f"### {spec.category}: {spec.name}"]
    if spec.description:
        parts.append(sentence(spec.description))
    parts.append(json_block(spec.value.model_dump(mode="json", by_alias=True)))
    return "\n\n".join(parts)


def render_presets(registry: Registry) -> str:
    grouped = builtin_presets(registry)
    index = table(["Category", "Model", "Presets"], [
        (f"[{category}](#{anchor(category)})", code(type(specs[0].value).__name__),
         ", ".join(code(spec.name) for spec in specs))
        for category, specs in grouped.items()
    ])
    blocks: list[str] = [INTRO, index]
    for category, specs in grouped.items():
        blocks.append(f"## {category}")
        if category in USES:
            blocks.append(USES[category])
        blocks.append(model_field_table(type(specs[0].value)))
        blocks.extend(render_preset(spec) for spec in specs)
    return page("Preset reference", *blocks)


__all__ = ["builtin_presets", "render_preset", "render_presets"]
