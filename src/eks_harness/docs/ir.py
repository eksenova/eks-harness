from __future__ import annotations

from typing import get_args

from pydantic import BaseModel

from eks_harness.docs.markdown import code, model_field_table, page
from eks_harness.ir import SCHEMA_ID
from eks_harness.ir.hashing import (
    NODE_HASH_EXCLUDED,
    NODE_ID_LENGTH,
    PLAN_HASH_EXCLUDED,
    RESOURCE_ID_LENGTH,
)
from eks_harness.ir.plan import (
    NODE_ID_PATTERN,
    RESOURCE_ID_PATTERN,
    Binding,
    CheckUse,
    Clock,
    FunctionSource,
    Node,
    Plan,
    PlanKind,
    Requirement,
    Resource,
    SessionSpec,
    SourceInfo,
)
from eks_harness.ir.refs import (
    FilterStep,
    FindStep,
    FmtExpr,
    ItemExpr,
    LenStep,
    RefExpr,
    SliceStep,
)
from eks_harness.ir.registry import BUILTIN_PLUGIN, Registry
from eks_harness.ir.time import TICKS_PER_SECOND
from eks_harness.ir.types import SCALAR_TYPES

INTRO = f"""A plan is the typed, serializable graph that `eks.plan(...)` builds from an entry function and the hub
validates and runs. Every surface works on the same JSON document: `eks-harness plan <file> --json` prints it,
`POST /api/plans` builds it on the hub, `POST /api/runs` runs it (or plans an entry file first), and the MCP tools
`eks_plan`, `eks_validate` and `eks_run` return or accept it.
The schema id is {code(SCHEMA_ID)}. The JSON Schema of the whole document, with one `oneOf` branch per registered op,
is printed by `eks-harness schema`; `eks-harness schema --out DIR` writes `DIR/plan.schema.json` and one
`DIR/ops/<op>.json` per op, and `eks-harness schema --op <name>` prints one op's argument schema."""
SHAPE = """```json
{
  "schema": "eks/plan@1",
  "kind": "flow",
  "name": "subeler",
  "params": {},
  "clock": {"now": "2026-01-15T09:00:00+03:00", "seed": 2026},
  "session": {"key": "main", "app": "web-app", "platform": "web", "seed": "standard", "persona": "harman-admin"},
  "resources": [
    {"id": "r3k9d2qa", "kind": "lease.web", "app": "web-app", "session": "main"},
    {"id": "r7m1x0vc", "kind": "backend", "app": "web-app", "definition": "eksenapi", "seed": "standard", "session": "main"}
  ],
  "nodes": [
    {"id": "n5c8w2k1d9q", "op": "api.get", "args": {"path": "/api/tenant-branches"}, "uses": ["r7m1x0vc"],
     "out": "json", "src": ".harness/flows/subeler.py:7", "hash": "sha256:..."},
    {"id": "n0t4b7r6y2m", "op": "ui.nav",
     "args": {"route": {"$fmt": ["/tenant/invoice/list?branchId=",
                                 {"$ref": "n5c8w2k1d9q", "path": ["items", {"find": {"name": "Kadıköy"}}, "id"]}]}},
     "uses": ["r3k9d2qa"], "src": ".harness/flows/subeler.py:13", "hash": "sha256:..."}
  ],
  "hash": "sha256:..."
}
```

The ids and hashes above are illustrative; real ones come from the hashing rules below."""
NODES_TEXT = """A node is one op call: `op` names a registered op, `args` is validated by the op's argument model,
`uses` lists the resources the node runs on and `after` the earlier nodes it waits for. Container ops ({containers})
hold child nodes in `branches`. Nodes on the same resource lane run in program order;
independent lanes and pure nodes may run in parallel. `pin` is the take time of a node recorded with `at=` inside a
score take."""
IDS_TEXT = f"""Node ids match {code(NODE_ID_PATTERN)}: `n` plus {NODE_ID_LENGTH} Crockford base32 characters derived
from the scope path, the call chain and the call's source text, so an id survives blank lines, comments and unrelated
edits, and plan diffs can match nodes across edits. Nodes expanded at run time (`eks.each` over a Ref, `eks.dynamic`)
get `.N` suffixes. Resource ids match {code(RESOURCE_ID_PATTERN)}: `r` plus {RESOURCE_ID_LENGTH} characters derived
from the resource kind and its identity."""
REFS_TEXT = """Ops return Refs in Python; in the plan a Ref is a JSON expression wherever an argument accepts one:

* `{{"$ref": "<node id>", "path": [...]}}` reads the output of an earlier node.
* `{{"$item": "<each node id>", "path": [...]}}` reads the current item of an enclosing `eks.each`.
* `{{"$fmt": ["text", {{"$ref": ...}}, "text"]}}` joins text and Refs into one string at run time. Python builds it from
  an f-string that interpolates Refs.

A path is a list of steps: an object key (string), a list index (integer), `{{"slice": [start, stop, step]}}`,
`{{"find": {{field: value}}}}` (the first element whose fields match), `{{"filter": {{field: value}}}}` (every matching
element) or `{{"len": true}}`. Ref types ({types}, `list[T]` and `resource:<kind>`) are checked when the plan is validated, and outputs are checked against the declared type when the node runs."""
TIME_TEXT = f"""Times serialize as their canonical grammar string, such as `"1.5s"`, `"300ms"`, `"f:120"`,
`"beat:8 + 1b"`, `"bar:4"`, `"cue:logo - 2f"`, `"marker:intro#2"` or `"word:'EksenGOLD'"`. Internally a time is an
integer number of ticks, {TICKS_PER_SECOND:,} per second, which divides evenly by the common frame rates and audio
sample rates, so frame times are exact. Symbolic times (`beat`, `downbeat`, `bar`, `cue`, `marker`, `word`, `start`,
`end`) resolve against the plan clock: its `fps`, `tempo`, `cues`, `window` and `duration`, and the `media.markers`
nodes that provide marker and word streams. Bare numbers are rejected wherever a time is expected."""
HASH_TEXT = """Plans are Merkle hashed with SHA-256 over canonical JSON (sorted keys, no whitespace, UTF-8, finite numbers
only, sets sorted):

* A node hash covers its op, op version, executor version, args (with every `$ref` replaced by the hash of the node
  it reads and every `$item` by its container depth), the hashes of the resources it uses, its `after` edges, `out`,
  `pin`, the hashes of its branch children and the part of the clock it depends on. Excluded: {node}.
* A resource hash covers every field but the id, plus the versions of the contributions that define its kind.
* The plan hash covers the schema id, kind, params, clock, session, resource hashes, root node hashes, outputs, seed
  personas, plugin requirements, base directory, the session presets and contribution kinds the plan uses.
  Excluded: {plan}.

Seeds for randomness derive from the root seed and a path: `derived_seed(root, path)` takes the first 8 bytes of
`sha256("<root>:<path>")`; a node's seed uses its hash as the path. The node hash, together with content digests of
the media it reads and the executor and machine facts that change its output, is the key of the engine's result
cache for pure nodes. See [determinism.md](../determinism.md) for what each executor guarantees."""
MODELS: tuple[tuple[str, type[BaseModel], str], ...] = (
    ("Plan", Plan, "The root document."),
    ("Clock", Clock, "Frozen wall clock, root seed and time base of the plan."),
    ("SessionSpec", SessionSpec, "The session of a flow plan, and of each take of a score."),
    ("CheckUse", CheckUse, "A check chosen for checkpoints, by kind with its parameters."),
    ("Resource", Resource, "A lease or backend the plan needs; the resource planner acquires them before the run."),
    ("Node", Node, "One op call."),
    ("Requirement", Requirement, "A plugin whose contributions the plan uses."),
    ("SourceInfo", SourceInfo, "Where the entry is defined."),
    ("FunctionSource", FunctionSource, "Source of an `eks.dynamic` function or a scene's `bpy` hook, run in the sandbox."),
    ("Binding", Binding, "A closure variable of a function source and how the sandbox rebuilds it."),
    ("RefExpr", RefExpr, "`{\"$ref\": ...}`: the output of an earlier node."),
    ("ItemExpr", ItemExpr, "`{\"$item\": ...}`: the current item of an `eks.each` container."),
    ("FmtExpr", FmtExpr, "`{\"$fmt\": [...]}`: text joined with Refs."),
    ("FindStep", FindStep, "Path step: first list element whose fields match."),
    ("FilterStep", FilterStep, "Path step: every list element whose fields match."),
    ("SliceStep", SliceStep, "Path step: a Python slice of a list."),
    ("LenStep", LenStep, "Path step: the length of the value."),
)


def render_ir(registry: Registry) -> str:
    containers = ", ".join(code(spec.name) for spec in sorted(registry.ops(), key=lambda spec: spec.name)
                           if spec.container and spec.provenance.plugin == BUILTIN_PLUGIN)
    types = ", ".join(code(name) for name in SCALAR_TYPES)
    kinds = ", ".join(code(kind) for kind in get_args(PlanKind))
    blocks = [
        INTRO,
        "## Shape",
        f"Plan kinds: {kinds}. A trimmed flow plan:",
        SHAPE,
        "## Nodes",
        NODES_TEXT.format(containers=containers),
        "## Ids",
        IDS_TEXT,
        "## Refs",
        REFS_TEXT.format(types=types),
        "## Time",
        TIME_TEXT,
        "## Hashing",
        HASH_TEXT.format(node=", ".join(code(name) for name in sorted(NODE_HASH_EXCLUDED)),
                         plan=", ".join(code(name) for name in sorted(PLAN_HASH_EXCLUDED))),
        "## Models",
        "Field tables of the IR models, generated from their pydantic definitions.",
    ]
    for title, model, description in MODELS:
        blocks.extend([f"### {title}", description, model_field_table(model)])
    return page("Plan IR", *blocks)


__all__ = ["render_ir"]
