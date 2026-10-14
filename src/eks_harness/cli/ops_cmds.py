from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape

from eks_harness.cli.ui import (
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    console,
    fail,
    kv_panel,
    print_json,
    table,
)

if TYPE_CHECKING:
    from eks_harness.ir.registry import OpSpec, Registry

DESCRIPTION_WIDTH = 80
PIPE_WIDTH = 240
FLAG_FIELDS = (
    ("pure", "pure"),
    ("idempotent", "idempotent"),
    ("deviceDependent", "device-dependent"),
    ("container", "container"),
    ("exportsBody", "exports-body"),
    ("pinnable", "pinnable"),
    ("recordingOnly", "recording-only"),
    ("bounded", "bounded"),
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "ops", help="the op catalog: every op with its family, platforms and argument schema",
        description="List the registered ops (built-in and from the tree's plugins), or show one op with its "
                    "argument JSON Schema.")
    parser.add_argument("name", nargs="?", help="an op name such as ui.tap; omit to list every op")
    parser.add_argument("--family", help="only ops of this family")
    parser.add_argument("--no-plugins", action="store_true", help="do not import the tree's plugin modules")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=cmd_ops)


def wide_console() -> Console:
    return console if console.is_terminal else Console(highlight=False, soft_wrap=True, width=PIPE_WIDTH)


def plain_rows(rows: Iterable[Sequence[Any]]) -> list[tuple[str, ...]]:
    return [tuple("" if value is None else escape(str(value)) for value in row) for row in rows]


def catalog_registry(no_plugins: bool) -> Registry:
    from eks.discovery import DiscoveryError, find_tree, load_plugins, tree_registry
    from eks_harness.ir.registry import RegistryError, default_registry, using_registry

    tree = find_tree(Path.cwd())
    registry = tree_registry(tree, plugins=not no_plugins) if tree is not None else default_registry()
    if not no_plugins:
        try:
            with using_registry(registry):
                load_plugins(tree)
        except (DiscoveryError, RegistryError) as problem:
            fail(str(problem), EXIT_USAGE)
    return registry


def find_op(registry: Registry, name: str) -> OpSpec:
    from eks_harness.ir.registry import RegistryError

    try:
        return registry.op(name)
    except RegistryError as problem:
        fail(str(problem), EXIT_NOT_FOUND)


def _flags(summary: Mapping[str, Any]) -> str:
    flags = [label for key, label in FLAG_FIELDS if summary.get(key)]
    if summary.get("ordering") == "graph" and not summary.get("pure"):
        flags.append("graph")
    return ", ".join(flags)


def _platforms(summary: Mapping[str, Any]) -> str:
    platforms = summary.get("platforms")
    return "any" if platforms is None else ", ".join(platforms)


def _plugin(summary: Mapping[str, Any]) -> str:
    version = summary.get("pluginVersion")
    return f"{summary['plugin']} {version}" if version else str(summary["plugin"])


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 3] + "..."


def _type_text(schema: Any) -> str:
    if not isinstance(schema, Mapping):
        return "any"
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "const" in schema:
        return repr(schema["const"])
    if "enum" in schema:
        return " | ".join(repr(value) for value in schema["enum"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join(dict.fromkeys(_type_text(option) for option in schema[key]))
    kind = schema.get("type")
    if kind == "array":
        return f"list[{_type_text(schema.get('items'))}]"
    if kind == "object" and "additionalProperties" in schema and schema["additionalProperties"] is not False:
        return f"dict[str, {_type_text(schema['additionalProperties'])}]"
    if isinstance(kind, str):
        return f"{kind} ({schema['format']})" if "format" in schema else kind
    return "any"


def _argument_rows(schema: Mapping[str, Any]) -> list[tuple[str, str, str, str, str]]:
    properties: Mapping[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    rows: list[tuple[str, str, str, str, str]] = []
    for name, field in properties.items():
        default = "" if name in required or "default" not in field else json.dumps(field["default"], ensure_ascii=False)
        rows.append((name, _type_text(field), "yes" if name in required else "", default,
                     str(field.get("description", ""))))
    return rows


def _show_one(registry: Registry, spec: OpSpec, as_json: bool) -> None:
    from eks_harness.ir.schema import op_schema, op_summary

    summary = op_summary(spec)
    schema = op_schema(spec, registry=registry)
    if as_json:
        print_json({**summary, "schema": schema})
        return
    details: list[tuple[str, Any]] = [
        ("family", summary["family"]),
        ("version", summary["version"]),
        ("out", summary["out"]),
        ("description", summary["description"]),
        ("location", summary["location"]),
        ("executor", summary["executor"] or ""),
        ("ordering", summary["ordering"]),
        ("platforms", _platforms(summary)),
        ("resources", ", ".join(summary["resources"])),
        ("branches", ", ".join(summary["branches"])),
        ("flags", _flags(summary)),
        ("plugin", _plugin(summary)),
    ]
    console.print(kv_panel(spec.name, [(key, escape(str(value))) for key, value in details]))
    rows = _argument_rows(schema)
    if rows:
        wide_console().print(table(["argument", "type", "required", "default", "description"], plain_rows(rows),
                                   title="arguments"))
    else:
        console.print("no arguments")
    for index, example in enumerate(schema.get("examples", [])):
        console.print(kv_panel(f"example {index + 1}", [("args", escape(json.dumps(example, ensure_ascii=False)))]))


def _show_all(registry: Registry, family: str | None, as_json: bool) -> None:
    from eks_harness.ir.registry import unknown_name
    from eks_harness.ir.schema import op_summary

    if family is not None and family not in registry.families():
        fail(str(unknown_name("op family", family, registry.families())), EXIT_NOT_FOUND)
    summaries = [op_summary(spec) for spec in registry.ops(family)]
    if as_json:
        print_json({"ops": summaries})
        return
    rows = [
        (item["name"], item["out"], item["location"], _platforms(item), _flags(item), _plugin(item),
         _clip(item["description"], DESCRIPTION_WIDTH))
        for item in summaries
    ]
    wide_console().print(table(["name", "out", "location", "platforms", "flags", "plugin", "description"],
                               plain_rows(rows),
                        title=f"{len(rows)} op{'' if len(rows) == 1 else 's'}"))


def cmd_ops(args: argparse.Namespace) -> int:
    registry = catalog_registry(args.no_plugins)
    if args.name:
        spec = find_op(registry, args.name)
        if args.family is not None and spec.family != args.family:
            fail(f"op {spec.name!r} is in family {spec.family!r}, not {args.family!r}", EXIT_NOT_FOUND)
        _show_one(registry, spec, args.json)
    else:
        _show_all(registry, args.family, args.json)
    return 0


__all__ = ["catalog_registry", "cmd_ops", "find_op", "register"]
