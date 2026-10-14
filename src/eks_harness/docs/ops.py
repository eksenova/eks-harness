from __future__ import annotations

import importlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eks_harness.docs.markdown import (
    anchor,
    cell,
    code,
    def_name,
    field_table,
    json_block,
    page,
    sentence,
    table,
    type_text,
)
from eks_harness.ir.registry import OpSpec, Registry
from eks_harness.ir.schema import OPS_DIR, write_schemas

SURFACE_MODULE = "eks_harness.ir.surface"
FLAGS = (
    ("pure", "pure"),
    ("idempotent", "idempotent"),
    ("device_dependent", "device-dependent"),
    ("container", "container"),
    ("exports_body", "exports-body"),
    ("pinnable", "pinnable"),
    ("recording_only", "recording-only"),
    ("bounded", "bounded"),
)
INTRO = """Every op a plan node can name, grouped by family. Each entry lists the Python call that records the op
in a traced function (when the op has one), the platforms it runs on (`any` when it is not tied to a platform),
where its executor runs, the resources it holds, the Ref type of its output and its argument model as a table.
Arguments typed `Ref` also accept a Ref built by an earlier op; `Time` arguments take the Time grammar
(`t("1.5s")` in Python, `"1.5s"` in JSON). The same data is available as JSON from `eks-harness ops <name> --json`
and as JSON Schema from `eks-harness schema --op <name>`."""
FLAG_NOTES = """Flags: `pure` ops have no side effects and their results are cached by content; `idempotent` ops are
retried after a retryable failure and can be polled by `expect.eventually`; `device-dependent` ops give
machine-specific output, so machine facts go into their cache keys; `container` ops hold child nodes in branches;
`exports-body` containers let later nodes reference the nodes inside them; `pinnable` ops accept `at=` inside a
score take; `recording-only` ops need an enclosing recording or take; `bounded` ops read the clock window and
duration (their output length follows them)."""


def surface_paths() -> dict[str, str]:
    try:
        module = importlib.import_module(SURFACE_MODULE)
    except ImportError:
        return {}
    surface: Mapping[str, Any] = getattr(module, "SURFACE", {})
    paths: dict[str, str] = {}
    for name, entry in surface.items():
        path = getattr(entry, "path", None)
        if isinstance(path, str) and path:
            paths[name] = path
    return paths


def op_schemas(registry: Registry) -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="eks-docs-") as folder:
        root = Path(folder)
        write_schemas(root, registry=registry)
        return {path.stem: json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((root / OPS_DIR).glob("*.json"))}


def builtin_ops(registry: Registry) -> list[OpSpec]:
    from eks_harness.ir.registry import BUILTIN_PLUGIN

    return sorted((spec for spec in registry.ops() if spec.provenance.plugin == BUILTIN_PLUGIN),
                  key=lambda spec: spec.name)


def flags_of(spec: OpSpec) -> str:
    return ", ".join(label for attribute, label in FLAGS if getattr(spec, attribute))


def platforms_of(spec: OpSpec) -> str:
    return "any" if spec.platforms is None else ", ".join(sorted(spec.platforms))


def summary_rows(spec: OpSpec, python: str | None) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if python:
        rows.append(("Python", code(python)))
    rows.extend([
        ("Platforms", platforms_of(spec)),
        ("Location", code(spec.location)),
        ("Resources", ", ".join(code(role) for role in spec.resources) or "none"),
        ("Out", code(spec.out)),
        ("Version", code(spec.version)),
    ])
    if spec.branches:
        rows.append(("Branches", ", ".join(code(branch) for branch in spec.branches)))
    flags = flags_of(spec)
    if flags:
        rows.append(("Flags", flags))
    return rows


def render_op(spec: OpSpec, schema: Mapping[str, Any], python: str | None) -> str:
    parts = [f"### {spec.name}"]
    if spec.description:
        parts.append(sentence(spec.description))
    parts.append(table(["Property", "Value"], summary_rows(spec, python)))
    parts.append(field_table(schema))
    for index, example in enumerate(spec.examples, start=1):
        label = "Example" if len(spec.examples) == 1 else f"Example {index}"
        parts.append(f"{label} (arguments as JSON):\n\n{json_block(dict(example))}")
    return "\n\n".join(parts)


TYPES_INTRO = """Models and value types the argument tables above refer to, collected from the op schemas. A field
typed `Ref` also accepts a Ref; union members of a polymorphic field are listed by their model names."""


def collect_types(schemas: Mapping[str, Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for name in sorted(schemas):
        for key, body in sorted(schemas[name].get("$defs", {}).items()):
            found.setdefault(def_name(key), body)
    return dict(sorted(found.items(), key=lambda item: (item[0].lower(), item[0])))


def render_type(name: str, body: Mapping[str, Any]) -> str:
    parts = [f"### {name}"]
    description = body.get("description")
    if isinstance(description, str) and description.strip():
        parts.append(cell(description))
    if isinstance(body.get("properties"), Mapping):
        parts.append(field_table(body, name="Field", empty="No fields."))
        variants = body.get("oneOf")
        if isinstance(variants, list) and variants:
            titles = [str(variant.get("title")) for variant in variants if isinstance(variant, Mapping) and variant.get("title")]
            if titles:
                parts.append("Kinds: " + ", ".join(code(title) for title in titles) + ".")
    else:
        parts.append("Type: " + code(type_text(body)))
    return "\n\n".join(parts)


def render_ops(registry: Registry) -> str:
    specs = builtin_ops(registry)
    paths = surface_paths()
    schemas = op_schemas(registry)
    families: dict[str, list[OpSpec]] = {}
    for spec in specs:
        families.setdefault(spec.family, []).append(spec)
    index = table(["Family", "Ops"], [
        (f"[{family}](#{anchor(family)})", ", ".join(f"[{spec.name}](#{anchor(spec.name)})" for spec in members))
        for family, members in sorted(families.items())
    ])
    blocks = [INTRO, FLAG_NOTES, f"{len(specs)} ops in {len(families)} families.", index]
    for family, members in sorted(families.items()):
        blocks.append(f"## {family}")
        blocks.extend(render_op(spec, schemas[spec.name], paths.get(spec.name)) for spec in members)
    types = collect_types({spec.name: schemas[spec.name] for spec in specs})
    blocks.extend(["## Types", TYPES_INTRO])
    blocks.extend(render_type(name, body) for name, body in types.items())
    return page("Op reference", *blocks)


__all__ = [
    "builtin_ops",
    "collect_types",
    "flags_of",
    "op_schemas",
    "platforms_of",
    "render_op",
    "render_ops",
    "render_type",
    "surface_paths",
]
