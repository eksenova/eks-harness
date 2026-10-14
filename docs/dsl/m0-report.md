# M0 report: authoring foundations

Status: M0 done. Every example plan of `design.md` builds through the CLI path
(`eks.workspace.plan_spec`, then `eks.validate`) with no errors and no warnings, and every built
plan validates against the exported plan JSON Schema. Verified on 2026-10-12 in the
`feature/unified-dsl` worktree (nothing committed).

Sources: [design.md](design.md) (the agreed design), [contracts-m0.md](contracts-m0.md) (the M0
contracts), [decisions-m0.md](decisions-m0.md) (every choice the design left open, about 217 rows).

## 1. What was built

M0 is the authoring side only: nothing executes, nothing talks to the hub, devices or backends.

### 1.1 Layers

| Layer | Package | Role |
| --- | --- | --- |
| IR (bottom, never imports `eks`) | `eks_harness.ir` | All data definitions: `Time`, `Target`, Ref encodings, plan models, op argument models, catalogs, presets, the registry store and the registration decorators |
| Validator | `eks_harness.engine` | `validator.py`: the M0 plan validator (no IO), wrapped by `eks.validate` |
| SDK | `eks` | Ref proxies, tracing, control flow, builders per surface, planning, discovery, workspace; re-exports the public names |
| CLI | `eks_harness.cli` | `plan_cmds.py`, `ops_cmds.py`, `schema_cmds.py`, registered in `cli/__init__.py` |

Size: `src/eks` 9 375 lines in 35 modules, `src/eks_harness/ir` + `engine` + the three CLI modules
12 188 lines. No comments, docstrings, TODOs or `NotImplementedError` remain in non-test code
(checked with a tokenizer and AST scan over all 81 files).

### 1.2 `eks_harness.ir`

| Module | Content |
| --- | --- |
| `time.py` | integer ticks (705 600 000 per second), `Time` with anchors and bar/beat/frame/tick offsets, the one grammar (`1.5s`, `f:120`, `beat:8`, `bar:4`, `cue:`, `marker:`, `word:`), canonical form, arithmetic, `Span`, `TimeSet`, tempo and meter maps, `TimeContext` resolution, the `t` factory |
| `target.py` | `Target` model and JSON envelope, built-in kinds (`test_id`, `role`, `text`, `label`, `placeholder`, `css`, `kind`, `point`, ...), `within`/`near`/`nth`/`first`/`last`, strict shorthand parser, per-platform capability tables, `by` |
| `refs.py`, `types.py` | `$ref`, `$item`, `$fmt` encodings, path steps (`find`, `filter`, slices, `first`, `last`, `len`), Ref type grammar and inference, `Artifact`, `MediaInfo`, `Color`, media URIs |
| `plan.py`, `hashing.py` | `Plan` (`eks/plan@1`), `Node`, `Resource`, `Clock`, `SessionSpec`, `FunctionSource`; canonical JSON, Merkle node and plan hashes, stable node and resource ids, derived seeds, `cache_inputs`/`cacheable` |
| `registry.py`, `entries.py`, `presets.py` | op/macro/resource/check/catalog/renderer/backend/preset specs, `Registry` with snapshot/rollback and plugin provenance, `Poly` polymorphic fields, the `flow`/`seed`/`edit`/`scene`/`score` entry decorators, built-in presets (pace, viewport, network profiles, overlay styles, personas) |
| `ops/*` | argument models for 100 built-in ops in 23 families: `ui` 34, `mobile` 13, `api` 5, `capture` 5, `ctl` 5, `media` 4, `net` 4, `overlay` 4, `web` 4, `check` 3, `inject` 3, `data` 2, `debug` 2, `expect` 2, `fake` 2, `audio`, `backend`, `blender`, `edit`, `gfx`, `scene`, `score`, `session` 1 each |
| `catalog/*` | effects, transitions, easings, curves, keyframes, animated values, markers, durations |
| `schema.py` | `op_schema`, `plan_schema` (draft 2020-12, `$id` `eks/plan@1`, one `oneOf` branch per op, 311 shared `$defs` for the built-ins), `write_schemas` |

### 1.3 `eks` public API

`import eks` exports 71 names, fixed in `src/eks/__init__.py` and listed in contracts-m0.md section 1:

| Group | Names |
| --- | --- |
| entries | `flow`, `seed`, `edit`, `scene`, `score`, `macro`, `plan`, `validate` |
| extensions | `op`, `resource`, `check`, `effect`, `transition`, `easing`, `curve`, `renderer`, `overlay_kind`, `target_kind`, `backend`, `backend_policy`, `preset`, `Args`, `PresetModel`, `refers_to` |
| values | `Ref`, `Json`, `JsonLike`, `Media`, `Persona`, `Artifact`, `Time`, `Span`, `TimeSet`, `t`, `Target`, `by`, `Plan` |
| control | `when`, `otherwise`, `each`, `parallel`, `dynamic` |
| assertions and media | `expect`, `annotate`, `overlay`, `checks`, `media`, `fx`, `tx`, `ease`, `curves`, `keys` |
| builder types | `Session`, `Data`, `Resource`, `ResourceRef`, `Edit`, `GfxScene`, `BlenderScene`, `WebScene`, `Score` |
| extension bases | `EffectBase`, `TransitionBase`, `EasingBase`, `CurveBase`, `OverlayBase`, `Pace`, `Viewport`, `NetProfile`, `OverlayStyle`, `PersonaPreset` |

Session surface (`s.ui`, `s.web`, `s.mobile`, `s.api`, `s.net`, `s.inject`, `s.fake`, `s.overlay`,
`s.debug`, `s.checkpoint`, `s.shot`, `s.dump`, `s.har`, `s.record`, `s.hold`, `s.as_`, `s.eval`,
`s.call`, `s.explore`), the seed graph (`eks.Data`), edits with tracks, clips, effects, transitions,
overlays, audio mix and review expectations, gfx/Blender/web scenes with the `@sc.bpy` escape hatch,
and scores with pinned takes are all traced into plans. `src/eks/py.typed` ships inline types.

Discovery and workspace: `eks.discovery` loads `.harness/{flows,seeds,edits,scenes,scores}` and
`<app>/.harness/flows`, app profiles (`app.toml`) and trusted repo plugins (`harness-plugin.toml`
`modules = [...]`, through `src/eks_harness/plugins/manifest.py`); `eks.workspace.plan_spec` is the
single path the CLI and the acceptance tests use.

### 1.4 CLI

| Command | Does |
| --- | --- |
| `eks-harness plan <file>[::name] [--set k=v] [--platform P] [--no-plugins] [--json]` | builds and validates; text table or `{"plan", "validation"}` JSON; exit 0 valid, 1 issues, 2 usage/build errors |
| `eks-harness ops [name] [--family F] [--no-plugins] [--json]` | op catalog; with a name, the summary plus its argument schema; unknown names exit 6 with suggestions |
| `eks-harness schema [--out DIR] [--op NAME] [--no-plugins] [--json]` | plan schema, one op schema, or `DIR/plan.schema.json` + `DIR/ops/<op>.json` |

## 2. Verification

| Check | Command | Result |
| --- | --- | --- |
| Full suite | `EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider` | 3812 passed, 6 skipped, 0 failed (232 s) |
| M0 suite | same, `tests/eks` | 2441 passed, 0 skipped, 74 test files (30 s); includes the pyright typing test (`test_typing_pyright.py`), which ran |
| Rest of the repo | same, `--ignore=tests/eks` | 1371 passed, 6 skipped |
| ruff | `uv run ruff check src/eks src/eks_harness/ir src/eks_harness/engine src/eks_harness/cli/{plan,ops,schema}_cmds.py src/eks_harness/plugins/manifest.py tests/eks examples/dsl` | all checks passed |
| pyright | `npx --yes pyright@1.1.390 --pythonpath .venv/bin/python src/eks src/eks_harness/ir src/eks_harness/engine src/eks_harness/cli/{plan,ops,schema}_cmds.py` | 0 errors, 0 warnings (76 files in `src/eks` + `ir`) |
| `ops` | in `examples/dsl`: `eks-harness ops`, `ops --json`, `ops ui.tap --json`, `ops ui.tapp` | exit 0; 101 ops (100 built-in + `cookbook.rate` from the example plugin); summary keys as contracted plus `schema`; unknown op exits 6 with "did you mean 'ui.tap' or 'ui.type' or 'ui.state'" |
| `schema` | `eks-harness schema`, `schema --op ui.nav`, `schema --out DIR` | exit 0; `$id` `eks/plan@1`, draft 2020-12, 317 `$defs` with the example plugin; 101 op files plus `plan.schema.json` |
| `plan` | every entry file under `examples/dsl/.harness`, text and `--json` | 24 plans, all exit 0, `ok: true`, 0 issues, and all 24 validate against the exported plan schema with `jsonschema.Draft202012Validator` |

The six skips are outside M0 and are optional heavy dependencies not installed on this machine:
`sam3` extra, `faster_whisper`, `rembg` (two tests), `df` (DeepFilterNet) and `demucs`.

### 2.1 Plans built from `examples/dsl`

| File | Design section | Plan |
| --- | --- | --- |
| `flows/subeler.py` | 4.1 | flow, 8 nodes, 2 resources |
| `flows/refs.py` | 4.2 | flow, 7 nodes |
| `flows/control.py` | 4.3 | flow, 13 nodes |
| `edits/timing.py` | 4.4 | edit, 9 nodes |
| `flows/targets.py`, `flows/targets_mobile.py` | 4.5 | flow, 13 nodes; mobile 6 nodes on `--platform ios` and `android` |
| `flows/surface_web.py`, `flows/surface_mobile.py` | 4.6 | flow, 69 nodes; mobile 19 nodes on `ios` and `android` |
| `seeds/standard.py`, `seeds/feature.py` | 7 | seed, 9 nodes; `d.extend` seed, 4 nodes |
| `flows/network.py` | 8 | flow, 7 nodes |
| `flows/debugging.py` | 9 | flow, 5 nodes |
| `flows/overlays.py`, `edits/annotated.py` | 10.1 | flow, 21 nodes; edit, 3 nodes |
| `edits/demo.py` | 10.2 | edit, 6 nodes |
| `edits/mix.py`, `scenes/titles.py`, `scenes/lower_third.py` | 10.3 | edit, 7 nodes; gfx scenes, 1 and 3 nodes |
| `scenes/phone.py` | 10.4 | Blender scene, 3 nodes (`--set screen=artifact:VID123`) |
| `scores/ad.py` | 10.5 | score, 12 nodes, 2 resources (take with pinned nodes, Blender scene, gfx, master loudness) |
| `flows/extending.py`, `edits/extending.py`, `plugins/cookbook` | 11 | flow, 11 nodes; edit, 2 nodes; plugin op, macro, resource, check, catalog entries, presets |

Three CLI calls exit 2 without extra arguments, by design: `surface_mobile.py` and
`targets_mobile.py` run on `["ios", "android"]` and need `--platform`, and `scenes/phone.py` has a
required `screen` parameter. With those arguments they plan and validate cleanly (rows above).

The acceptance tests (`tests/eks/test_examples_design.py` over the fixture tree
`tests/eks/examples/tree`, and `tests/eks/test_design_examples.py` over `examples/dsl`) additionally
check op sequences and key args per example, determinism (same JSON and hash twice, same ids and hashes
after blank lines, comments and renames), JSON round trips, schema validity and frozen clocks.

## 3. Decisions

All choices the design left open are in [decisions-m0.md](decisions-m0.md). The ones that shape the
public behaviour most:

* Layering: data definitions in `eks_harness.ir` (never imports `eks`), the SDK in `eks`.
* `Time` serializes as its canonical grammar string; builders accept `Time` instances only, `--set`
  and JSON parse strings; beat, bar, marker and word indices are 0-based.
* Target shorthand: `#id` is a test id, `prefix=rest` picks a kind, everything else is exact text; no
  CSS guessing.
* Node ids are `n` + 10 base32 characters derived from scope path, call chain and the call's source
  text (stable under blank lines, comments and unrelated edits); resource ids are content derived
  (`r` + 7 characters) instead of the design's illustrative `r1`, `r2`.
* Hashes cover op, versions, args (refs replaced by hashes), uses, after, out, pin, branches and the
  clock the node depends on; names, tags and `src` are not hashed.
* Type stubs: inline types with `py.typed`, no separate `.pyi` files; plugin ops are typed through
  their argument models (`s.call(Model(...))`).

## 4. Not done, and known leftovers

Out of M0 scope by the design's milestones (not attempted):

* Execution of any kind: engine scheduler, journal, cache, resource planner, hub `/api/plans` and
  `/api/runs`, the `run`, `record`, `debug`, `runs` and `do` commands, MCP tools (M1 onward).
* `eks-harness plan --diff <git-ref>` (plan diff is M6 tooling; recorded in decisions-m0.md).
* Executors for declared-only ops such as `check.explore` (M6), worker `defineOp` and TS types (M2),
  removal of the old APIs (M7), skills and generated docs (M7).

Leftovers found during verification, not caused by M0 code and not changed:

* `uv run ruff check src/eks_harness/cli/__init__.py` reports 2 `BLE001` and `src/eks_harness/cli/ui.py`
  reports `PYI041` and `UP037`; the same findings exist at `HEAD`. A repo-wide `ruff check .` reports
  1084 pre-existing findings outside the M0 paths. The repo is not `ruff format`-ed and M0 does not
  start doing so.
* `tests/eks/.ruff_cache/` is an untracked, non-ignored ruff cache created by a ruff run from inside
  `tests/eks`; it should be deleted before committing (left in place here because this verification
  did not create it).
* Changes outside the M0 packages that belong to the M0 work and should be reviewed with it:
  `pyproject.toml` and `uv.lock` (wheel packages, Python 3.13), `tests/conftest.py` (the `eks_registry`
  fixture), `src/eks_harness/cli/__init__.py` (registers the three commands),
  `src/eks_harness/plugins/manifest.py` (plugin `modules`), and `src/eks_harness/cli/ui.py`, whose
  `ok`/`warn`/`error`/`info` helpers now escape rich markup in messages (no existing caller passes
  markup to them; checked by grep).

## 5. Closeout

Final verification on 2026-10-12, after the critic rounds 6 to 9 that followed the report above. Where the
numbers below differ from sections 1 and 2, these are the current ones.

### 5.1 Gaps closed

* Since the first report, 36 rows were added to [decisions-m0.md](decisions-m0.md) (lambda curves, scene
  seeds, pin bounds, the sandbox, kind-dispatched schema branches, contribution hashing, take placement and
  critic rounds 6 to 9: clock bounds and providers in render hashes, analyses and device-dependent inference
  in cache keys, checked node outputs, after edges, seed paths, session identity, scene graph invariants,
  one registry per tree, unique marker names), each with its tests under `tests/eks`.
* The `# noqa` suppressions in `examples/dsl` are gone, fixed at their cause: `seeds/standard.py` and
  `edits/demo.py` no longer bind values they never read (F841), `flows/subeler.py` imports only `by` and
  `expect` in sorted order (F401, I001), and `flows/control.py` names the count Ref before the
  `@eks.dynamic` function instead of calling in its default (B008). The example files also carry no
  comments any more. Recorded as the decision row "Example files are lint-clean code (closeout)".
* `tests/eks/.ruff_cache/` is deleted.

### 5.2 Final counts

| Check | Result |
| --- | --- |
| Full suite, `EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider` | 4326 passed, 6 skipped, 0 failed (284 s); the skips are the six optional heavy dependencies of section 2, the two warnings are `audioread` deprecation warnings in `tests/video` |
| M0 suite, same on `tests/eks` | 2955 passed, 0 skipped, 92 test files (78 s) |
| ruff on `src/eks`, `ir`, `engine`, the three CLI modules, `plugins/manifest.py`, `tests/eks`, `examples/dsl` (`--no-cache`) | all checks passed |
| pyright 1.1.390 on `src/eks`, `ir`, `engine`, the three CLI modules | 0 errors, 0 warnings (82 files) |
| pyright 1.1.390 on `tests/eks` | 0 errors, 0 warnings (93 files) |
| pyright 1.1.390 on the 24 `examples/dsl` Python files named explicitly | 0 errors, 0 warnings |
| `eks-harness plan` on every entry file under `examples/dsl/.harness`, text and `--json` | 24 plans (19 files as they are, `surface_mobile.py` and `targets_mobile.py` with `--platform ios` and `--platform android`, `scenes/phone.py` with `--set screen=artifact:VID123`), all exit 0, `ok: true`, 0 issues; all 24 validate against the exported plan schema with `jsonschema.Draft202012Validator`. Without those arguments the three files exit 2 by design |
| `eks-harness ops` in `examples/dsl` | 102 ops (101 built-in, `data` now has 3: `data.base`, `data.create`, `data.extend`; plus `cookbook.rate`); `--no-plugins` 101; `ops ui.tapp` exits 6 |
| `eks-harness schema --out DIR` in `examples/dsl` | exit 0; `plan.schema.json` with 361 `$defs`, one op file per op |
| Source scan (tokenizer and AST) over the 82 M0 files, 25 866 lines | no comments, docstrings, TODO/FIXME, `NotImplementedError`, `# noqa`, type-ignore or em dash; the only `...` bodies are `@overload` stubs and `Protocol` members. The M0 lines added to `cli/__init__.py`, `cli/ui.py` and `plugins/manifest.py` carry no comments either |

Size now: `src/eks` 10 036 lines in 35 modules; `src/eks_harness/ir` and `engine` (44 modules) plus the
three CLI modules 15 830 lines; `import eks` exports 73 names.

### 5.3 Still open

* Everything listed as out of scope in section 4 (execution, `plan --diff`, executors, worker
  `defineOp`, TS types, removal of the old APIs, skills and docs) is unchanged and belongs to M1 onward.
* `src/eks_harness/cli/__init__.py` (2 `BLE001`) and `src/eks_harness/cli/ui.py` (`PYI041`, `UP037`)
  still report the four ruff findings that exist identically at `HEAD`; M0 does not touch them.
* The example files now differ from the design's code blocks in the three places recorded in the
  closeout decision row and in dropping the design's inline remarks; design.md itself is unchanged.
* Test files keep 22 `# noqa` markers (13 files), mostly deliberate patterns under test such as call
  defaults for `@eks.dynamic`; the no-suppression rule covers non-test code only.
* Nothing is committed; the worktree changes listed in section 4 still need review with the M0 work.
