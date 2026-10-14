from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from eks_harness.ir.registry import Registry

REFERENCE_FILES = ("catalog.md", "checks.md", "cli.md", "ir.md", "ops.md", "presets.md", "resources.md")


def builtin_registry() -> Registry:
    from eks_harness.ir.registry import base_registry

    return base_registry()


def generate(registry: Registry | None = None) -> dict[str, str]:
    from eks_harness.docs.catalog import render_catalog
    from eks_harness.docs.checks import render_checks
    from eks_harness.docs.cli import render_cli
    from eks_harness.docs.ir import render_ir
    from eks_harness.docs.ops import render_ops
    from eks_harness.docs.presets import render_presets
    from eks_harness.docs.resources import render_resources

    chosen = registry if registry is not None else builtin_registry()
    renderers: dict[str, Callable[[], str]] = {
        "catalog.md": lambda: render_catalog(chosen),
        "checks.md": lambda: render_checks(chosen),
        "cli.md": render_cli,
        "ir.md": lambda: render_ir(chosen),
        "ops.md": lambda: render_ops(chosen),
        "presets.md": lambda: render_presets(chosen),
        "resources.md": lambda: render_resources(chosen),
    }
    return {name: renderers[name]() for name in REFERENCE_FILES}


def write_reference(out: Path, registry: Registry | None = None) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in generate(registry).items():
        target = out / name
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def check_reference(out: Path, registry: Registry | None = None) -> list[str]:
    differing: list[str] = []
    for name, text in generate(registry).items():
        target = out / name
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            differing.append(name)
    return differing


__all__ = ["REFERENCE_FILES", "check_reference", "generate", "write_reference"]
