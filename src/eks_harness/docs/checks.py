from __future__ import annotations

from eks_harness.docs.markdown import (
    anchor,
    code,
    field_table,
    model_schema,
    page,
    sentence,
    table,
)
from eks_harness.ir.registry import BUILTIN_PLUGIN, CheckSpec, Registry

FAMILIES = (
    ("checkpoint", "Checkpoint checks",
     ("Run by `s.checkpoint(label)` (op `check.checkpoint`) on the session's screen. Checks marked default run on "
     "every checkpoint that has no `checks=` of its own; `checks=[eks.checks.use(kind, **params)]` picks others. "
     "A failed check fails the run unless the checkpoint is soft or the message matches the session's noise list.")),
    ("visual", "Visual regression checks",
     ("Golden comparisons at a checkpoint. Executors answer `check_unsupported` until the visual regression "
     "tooling lands.")),
    ("perf", "Performance checks",
     ("Performance budgets at a checkpoint. Executors answer `check_unsupported` until the performance tooling "
     "lands.")),
    ("media", "Media checks",
     ("Run by `check.media` on a rendered or captured media file. In an edit they are queued with "
     "`e.expect.<kind>(...)`, or derived from the timeline with `e.expect.derive(...)`.")),
)
INTRO = """Every check kind the built-in registry knows, by family, with its parameters. Plugins add kinds with
`@eks.check(kind, family=...)`."""


def builtin_checks(registry: Registry, family: str) -> list[CheckSpec]:
    return sorted((spec for spec in registry.checks() if spec.family == family
                   and spec.provenance.plugin == BUILTIN_PLUGIN), key=lambda spec: spec.kind)


def check_rows(spec: CheckSpec) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = [
        ("Family", code(spec.family)),
        ("Model", code(spec.args.__name__)),
        ("Platforms", "any" if spec.platforms is None else ", ".join(sorted(spec.platforms))),
    ]
    if spec.family != "media":
        rows.append(("Default", "yes" if spec.default else "no"))
    if spec.device_dependent:
        rows.append(("Device dependent", "yes"))
    return rows


def render_check(spec: CheckSpec) -> str:
    parts = [f"### {spec.kind}"]
    if spec.description:
        parts.append(sentence(spec.description))
    parts.append(table(["Property", "Value"], check_rows(spec)))
    parts.append(field_table(model_schema(spec.args), name="Parameter", empty="No parameters."))
    return "\n\n".join(parts)


def render_checks(registry: Registry) -> str:
    index_rows = []
    blocks: list[str] = []
    for family, title, intro in FAMILIES:
        specs = builtin_checks(registry, family)
        index_rows.append((f"[{title}](#{anchor(title)})", ", ".join(code(spec.kind) for spec in specs)))
        blocks.extend([f"## {title}", intro])
        blocks.extend(render_check(spec) for spec in specs)
    return page("Check reference", INTRO, table(["Family", "Kinds"], index_rows), *blocks)


__all__ = ["builtin_checks", "render_check", "render_checks"]
