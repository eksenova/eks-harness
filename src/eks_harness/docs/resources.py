from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, get_args

from eks_harness.docs.markdown import (
    code,
    field_table,
    model_schema,
    page,
    sentence,
    table,
)
from eks_harness.docs.ops import builtin_ops, op_schemas, render_op, surface_paths
from eks_harness.ir.registry import BUILTIN_PLUGIN, Phase, Registry, ResourceSpec

DATA_FAMILY = "data"
CONTEXT_MEMBERS: tuple[tuple[str, str], ...] = (
    ("kind", "Resource kind of this resource"),
    ("key", "Key of this resource (`None` for a keyless kind)"),
    ("path", "Path of this resource in the seed graph, such as `account:acme/user:acme-admin`"),
    ("phase", "Phase this resource runs in"),
    ("parent", "Output of the parent resource; `None` for a root kind"),
    ("fields", "The resource's fields with references replaced by the outputs they point at"),
    ("resolve(value)", ("Output of a referenced resource: pass a field value of `self` (a reference); any other value "
                       "comes back as it is")),
    ("api", "`SeedApi` on the backend URL (methods below); fails with `SeedError` when the backend has no URL"),
    ("backend", "`BackendInfo` of the backend with its revealed values: url, credentials, env, extra"),
    ("now", "The seed's frozen `now` as an aware `datetime`"),
    ("seed", "Seed derived from the seed's root seed and this resource's path"),
    ("random", "`random.Random` seeded with `seed`"),
    ("ids", "`Ids` generator on `seed` and `now` (methods below); the n-th call of a resource is stable"),
    ("log(message)", "A line in the run journal (seed runs) or in the seed resource record (builds inside a flow)"),
)
SEED_HOOKS: tuple[tuple[str, str], ...] = (
    ("reset(context)", ("Bring the store to its empty, migrated state. A build needs it; without it the engine builds "
                       "only into a fresh backend generation")),
    ("seed_phase(context, phase)", "Prepare for a phase; called once before the first resource of each phase"),
    ("snapshot(context, name)", ("Save the current state under `name`; may return `{\"digest\": \"sha256:...\"}`, a "
                                "digest of the logical content used by the determinism report")),
    ("restore(context, name)", "Load the state saved under `name`"),
    ("migrations(context)", ("A text that changes exactly when the schema changes; it replaces the backend fingerprint "
                            "in the snapshot key")),
    ("drop_snapshot(context, name)", "Delete a saved state; called when old snapshots are pruned"),
)
INTRO = """Seeds are resource graphs. An `@eks.seed` function declares resources on `eks.Data` (`d.<kind>(...)`,
children through `parent.<kind>(...)`), the plan records each one as a `data.create` node, and the seed engine creates
them through the kind's `create(self, ctx)` hook, snapshots the backend and restores the snapshot into later backend
leases. This page lists the data ops, the resource hook contract and the backend hooks the seed engine calls."""
CREATE_TEXT = """A resource kind is an `eks.Resource` model registered with
`@eks.resource(kind, parent=, phase=, keyed=, positional=, persona=, description=, version=)`. Its
`create(self, ctx)` method creates the resource and returns its output, a JSON object (or `None` for `{}`). `self`
is the model validated from the node's fields, so reference fields still hold their reference; `ctx.resolve(field)`
gives the referenced output.

* The output must be JSON; anything else fails the resource with `seed_error`.
* A kind registered with `persona=True` returns credentials in a form persona auth reads (`headers`, `token`,
  `username` and `password`, or `api_key` with `header`) plus what the app login needs (`tenant`, `roles`,
  `label`). The engine records them as the seed's personas under the resource key.
* Journals show outputs with `password`, `token`, `secret`, `api_key`, `apiKey`, `headers` and `authorization`
  replaced by `<redacted>`; snapshot manifests keep them (file mode 0600).
* A kind without `create` fails at run time with `seed_error` naming the kind."""
PHASE_TEXT = """Resources run one at a time, phase by phase, in program order inside a phase (parents and referenced
resources are declared first). Every `data.extend` runs before the first `data.create`. `mongo` resources hold data
that lives outside the backend snapshot: they run after the other phases and again after every restore, with the
recorded fields, and must give the same output each time."""
HOOKS_TEXT = """Optional methods of a backend implementation (`@eks.backend` classes and legacy plugin backends), called
in the backend's own process through the pool runner with the backend context. A backend that lacks a hook answers
`{"missing": true}` and the engine works around it: without `snapshot` and `restore` every restore is a build."""
KEYS_TEXT = """The snapshot key covers the seed plan hash (which covers its bases through `data.extend`), the backend
definition, its plugin and version, and the backend's `migrations` answer (or its fingerprint when it has no
`migrations` hook). Manifests live under `<state>/seeds/<definition>/<key>.json`; `seeds.keepSnapshots` (default 3)
bounds how many are kept per definition and seed. `eks-harness seed snapshots`, `seed show <key>` and
`seed drop <key>` inspect and remove them, and `eks-harness run ... --seed restore|fresh|rebuild|skip` picks how a
run uses them."""


def builtin_resources(registry: Registry) -> list[ResourceSpec]:
    return sorted((spec for spec in registry.resources() if spec.provenance.plugin == BUILTIN_PLUGIN),
                  key=lambda spec: spec.kind)


def method_rows(cls: type, names: tuple[str, ...]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for name in names:
        member = getattr(cls, name)
        signature = str(inspect.signature(member)).replace("(self, ", "(").replace("(self)", "()")
        rows.append((code(f"{name}{signature}"), ""))
    return rows


def public_methods(cls: type) -> tuple[str, ...]:
    return tuple(sorted(name for name, member in vars(cls).items()
                        if not name.startswith("_") and inspect.isfunction(member)))


def render_kind(spec: ResourceSpec) -> str:
    rows = [("Model", code(spec.model.__name__)), ("Parent", code(spec.parent) if spec.parent else "none"),
            ("Phase", code(spec.phase)), ("Keyed", "yes" if spec.keyed else "no"),
            ("Persona", "yes" if spec.persona else "no")]
    parts = [f"### {spec.kind}"]
    if spec.description:
        parts.append(sentence(spec.description))
    parts.append(table(["Property", "Value"], rows))
    parts.append(field_table(model_schema(spec.model), name="Field", empty="No fields."))
    return "\n\n".join(parts)


def render_resources(registry: Registry) -> str:
    from eks_harness.data.context import SeedApi
    from eks_harness.data.ids import Ids

    schemas: Mapping[str, Any] = op_schemas(registry)
    paths = surface_paths()
    data_ops = [spec for spec in builtin_ops(registry) if spec.family == DATA_FAMILY]
    kinds = builtin_resources(registry)
    blocks: list[str] = [INTRO, "## Data ops"]
    blocks.extend(render_op(spec, schemas[spec.name], paths.get(spec.name)) for spec in data_ops)
    blocks.extend(["## Resource kinds", CREATE_TEXT])
    if kinds:
        blocks.extend(render_kind(spec) for spec in kinds)
    else:
        blocks.append("The built-in registry defines no resource kinds; projects and plugins register their own.")
    blocks.extend([
        "## Phases",
        "Phases in order: " + ", ".join(code(phase) for phase in get_args(Phase)) + ".",
        PHASE_TEXT,
        "## ResourceContext",
        "The `ctx` argument of `create(self, ctx)`.",
        table(["Member", "Meaning"], [(code(name), meaning) for name, meaning in CONTEXT_MEMBERS]),
        "### SeedApi",
        ("`ctx.api`: JSON requests against the backend URL. Each returns the parsed body; a status outside 2xx "
        "(or outside `expect=`) raises `SeedError` with the method, URL, status and a body excerpt. `as_=` "
        "authenticates as a persona this seed or its bases created, or as a backend persona."),
        table(["Method", "Notes"], method_rows(SeedApi, ("get", "post", "put", "patch", "delete", "request"))),
        "### Ids",
        ("`ctx.ids`: deterministic identifiers. TCKN and VKN carry valid checksums, `iban` is a Turkish IBAN valid "
        "under mod 97, `ulid` uses the frozen `now`."),
        table(["Method", "Notes"], method_rows(Ids, public_methods(Ids))),
        "## Backend seed hooks",
        HOOKS_TEXT,
        table(["Hook", "Contract"], [(code(name), meaning) for name, meaning in SEED_HOOKS]),
        "## Snapshots",
        KEYS_TEXT,
    ])
    return page("Resource reference", *blocks)


__all__ = ["CONTEXT_MEMBERS", "SEED_HOOKS", "builtin_resources", "public_methods", "render_resources"]
