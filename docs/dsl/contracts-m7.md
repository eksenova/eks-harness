# M7 contracts: surfaces and docs

M7 finishes the agent and user surfaces on the new API (MCP, CLI, the Claude Code plugin's skills, agents,
commands and hooks), generates the reference from the registry, documents the cookbook, determinism, the plan IR and
the migration, and removes the old APIs that have a replacement. Done when no references to removed APIs remain.

M5 (scenes and scores) and M6 (tooling) are not done. Old code that only they replace stays and is listed in
section 6; it is not a removed API.

## 1. MCP

Final tool set:

| Group | Tools |
| --- | --- |
| Runs and plans (M1, kept) | `eks_run`, `eks_plan`, `eks_validate`, `eks_ops`, `eks_do`, `eks_run_status`, `eks_run_control`, `eks_flows`, `eks_flow_get`, `eks_flow_save`, `eks_flow_delete` |
| Sessions (new) | `eks_session_open`, `eks_session_do`, `eks_session_close`, `eks_session_export` |
| Seeds (new) | `eks_seeds` (list snapshots), `eks_seed_show` |
| Store (kept) | `list_projects`, `list_sessions`, `list_artifacts`, `get_artifact`, `upload_artifact`, `share_artifact`, `mark_seen`, `tag_artifacts`, `set_artifact_retention`, `search`, `add_note`, `session_timeline`, `lease_status`, and the annotation store tools `annotate`, `annotate_rerender`, `annotate_recapture`, `list_annotation_versions`, `restore_annotation_version` |
| System (kept) | plugins, nodes and jobs, backends and backend leases (`backend_*`, `lease_*`, `choose_backend`, `list_backend_definitions`, `list_backends`), `render_queue`, `driver_workers`, `video_sheet`, `video_check` (artifact review) |
| Kept until M5 | `score_validate`, `score_plan`, `score_export`, the studio and legacy video tools of `mcp_video.py` |

Removed: `driver_act`, `driver_observe`, `driver_capture`, `flow_run`, `seed_backend`, `list_personas` (seed personas
come from `eks_seed_show`), `ensure_backend` (use `backend_acquire`).

Sessions (`mcp_engine.py`, `api/routes_sessions_engine.py`: `POST /api/engine/sessions`, `POST /api/engine/sessions/{id}/do`, `DELETE /api/engine/sessions/{id}`, `GET /api/engine/sessions/{id}/export`; under `/api/engine` because `/api/sessions` is the store's):

* `eks_session_open(app, platform=None, seed=None, as_=None, backend=None, viewport=None, tree=None)` starts a debug
  run of a generated flow whose only node is `s.debug.break_("session")` with the given session options, waits for
  its pause and returns `{session: <run id>, pause}`. The run's pause timeout is `engine.sessionTimeoutSeconds`
  (default 1800 s); every `eks_session_do` renews it.
* `eks_session_do(session, op, args)` runs `{op, args}` through the debugger's `do` action (contracts-m1 5.11) and
  returns the value and the artifacts it made.
* `eks_session_close(session)` continues the run, which ends `passed`, and releases its leases.
* `eks_session_export(session, name=None)` turns the session's ad-hoc ops, in order, into a `.py` flow: each op
  becomes its Python call through `eks_harness/ir/surface.py` (op name to `s.<path>(...)` call with positional and
  keyword arguments); an op without a surface entry becomes `s.op("<op>", {...})`. The result is planned and
  validated before it is returned.

`eks_harness/ir/surface.py` maps every built-in op to its authoring call (`ui.tap` -> `s.ui.tap(target)`, `api.get`
-> `s.api.get(path, ...)`, ...); a test checks that every mapped attribute path exists on the builders and that every
op of a session family has an entry. `s.op(name, args)` becomes public (`Session.op`), recording any registered op
with literal args.

## 2. CLI

Removed: `flow run|list` (use `run`, and `eks-harness plan` or `eks_flows` to list). Kept with the M5 code: `score`,
`video`, `studio`, `queue`. New: `eks-harness docs [--out DIR] [--check]` (section 4). `review_cmds` keeps `video sheet`
and `video check`; their `flows.client.download` helper moves to `cli/client.py`. The CLI description no longer
advertises flows.

## 3. Claude Code plugin

Skills (`claude/skills/`), each a `SKILL.md` with frontmatter and the API it documents taken from the code and the
generated reference:

| Skill | Covers |
| --- | --- |
| `eks-harness` | overview, routing to the other skills, the plan model in one page |
| `eks-dsl` | plans, Refs, Time, Targets, control flow, determinism rules |
| `eks-flows` | sessions, ui/web/mobile ops, expect, checkpoints, recordings, overlays, native surfaces, app runtimes |
| `eks-data` | seeds, resource kinds, personas, backends, dependencies, leases, snapshots |
| `eks-network` | routes, shaping, cassettes, injection |
| `eks-debugging` | breakpoints, pause, resume, ad-hoc ops, journals, `--repeat`; the recorder and plan debugger UI are M6 |
| `eks-checks` | checkpoint checks, media checks, derived review; visual, perf and explore are M6 |
| `eks-edit` | edits, effects, transitions, video overlays, review expectations, render settings |
| `eks-gfx-audio` | gfx layers and the audio mix |
| `eks-scenes` | Blender and web scenes as they are today; the M5 scene graph is marked as coming |
| `eks-scores` | the score clock; pinned takes are M5 |
| `eks-extending` | ops, macros, resources, checks, effects, renderers, presets, backends, app runtimes, JS `defineOp`, plugin manifests |
| `eks-mcp` | agent workflow: one-shot ops, sessions and export, runs, store tools |
| `eks-migration` | old API to new API mapping |
| `troubleshooting` | rewritten for the new API |

Old skills documenting removed APIs (`flows`, `web`, `ios`, `android`, `react-native`, `annotation`) are removed or
folded into the new ones; `devices`, `evidence`, `nodes`, `backends`, `video-review` are updated to the new API;
`video-editing`, `blender-scenes`, `web-scenes`, `scores` stay for the M5 code with a pointer to `eks-edit`. Agents
(`web-harness`, `ios-harness`, `android-harness`, `studio`) and commands (`/flow`, `/render`, `/score`, `/harness`)
are rewritten for `eks-harness run` and the `eks_*` tools. Hooks: `harness_hint`, `prewarm`, `evidence_guard`,
`pr_evidence_guard` and `cheap_inspection_guard` read `eks-harness run` output and the `eks_*` tools instead of
`flow run`, `flow_run` and `driver_*`.

## 4. Generated reference and docs

* `eks_harness/docs/` generates Markdown from the registry and the JSON Schemas: `docs/reference/ops.md` (every op by
  family: description, Python call from `ir/surface.py`, platforms, location, resources, out type, an argument
  table from `op_schema` with types, defaults and descriptions, and its examples), `catalog.md` (effects,
  transitions, easings, curves, overlays, marker sources, targets), `checks.md`, `presets.md`, `resources.md` (the
  data ops and resource hooks), `ir.md` (the plan IR schema in prose plus the JSON Schema link), `cli.md` (from the
  argparse tree). `eks-harness docs --out docs/reference` writes them; `--check` exits 1 when the files differ. A test
  runs the check, so the reference cannot drift.
* Hand-written: `docs/README.md` (index), `docs/cookbook.md` (every cookbook edit, flow and seed with what it shows
  and its source), `docs/determinism.md` (design 6 as the user contract, with what each executor guarantees),
  `docs/migration.md` (old API to new API, for phase 2 and external users), `docs/overview.md` replacing
  `docs/spec.md`, and a rewritten root `README.md`. `docs/drivers.md` is rewritten for the typed op protocol.

## 5. Removal (replacement exists)

* Python: `cli/flow_cmds.py`, `drivers/builtin.py`, `drivers/registry.py` `driver_for`, the act/observe/capture/events
  routes of `api/routes_drivers.py` (`/workers` stays), `capture/pace.py`, `capture/steplog.py` (the encoder reads the
  worker's step log through `capture/trim.py` `step_times`), the MCP tools of section 1. `drivers/client.py` `alive`
  uses `GET /health`.
* Workers: the legacy per-helper `ROUTES` (`/status`, `/goto`, `/click`, ...), extension routes
  (`EXTENSION_ROUTES`), the duplicated pace defaults (`PACE_FAST`, `PACE_ENV`; one `kit/src/pace.mjs` remains); `nav`,
  `reload`, `back` helpers the typed ops use are kept as plain functions.
* Plugin contribution types with no consumer: `platform`, `device_pool`, `app_profile`, `login`, `fake`, `capture`,
  `annotator`, `mcp_resources`, `skill`, `hook`, `agent`, `template`, `driver`; the matching scaffold templates;
  `@eks.op` and `@eks.macro` templates are added.
* Moved to M5 with the takes (section 6): `/exec` and the other `RUN_ROUTES`, `workers/kit/src/script-runner.mjs`,
  `flows/`, `drivers/sessions.py` (`WorkerSession`), `drivers/client.py` `exec`/`call` and the `flow_helpers`
  contribution type.
* Web UI: `features/ToolRail.tsx` and its API calls.
* Tests of removed code are removed; tests touching it partly are updated.

## 6. Kept until M5 and M6

`score/` and its CLI, routes, MCP tools and web page; the device-take cue model and plugin; Blender and web-scene
media plugins; the render farm and node jobs; `studio/` and its web pages; the legacy `video` authoring API, CLI and
orchestrator (also used by eksenads until phase 2); `routes_annotate` versions, recapture and project assets with
`annotate/` (engine internals of the overlay renderers); plan diff (`eks_diff`, `plan --diff`), the recorder and the
run page UI (M6).

* The runtime of the device takes, the score takes and the live score rehearsal stays until M5 replaces the takes
  with pinned flows: `flows/` (`WebApp`, `MobileApp`, `prepare_app`), `drivers/sessions.py` (`WorkerSession`),
  `drivers/client.py` `exec`/`call`, the `flow_helpers` contribution type, the worker `RUN_ROUTES` (`/run`, `/exec`,
  ...) and `workers/kit/src/script-runner.mjs`, with their tests. The device-take plugin hands a take flow the old
  `App` API (`flow(app, cue)`), and `score/takes.py` and `api/routes_scores.py` drive takes through `WorkerSession`;
  all of it runs scripts through `/exec`. Porting it changes the take-flow contract, which is M5 work.

## 7. Exit check

`scripts/check_removed.py` (and a test running it) searches `src/`, `workers/`, `sdk/`, `web/src/`, `claude/`,
`docs/` (except `docs/dsl/`, the planning record, `docs/migration.md` and `claude/skills/eks-migration/`, which name
the old API by design), `examples/` and `tests/` for the removed names (module paths, classes, routes, tool names, CLI
commands) and fails on any hit outside a narrow allowlist. The allowlist names the exact files (and, where only part of
a file is kept, the exact lines) of the section 6 take runtime, reason "kept until M5: device and score takes run on
it", and the MCP tests' assertions that the removed tools are absent, reason "asserts the tool is gone"; the script
prints the allowlisted count per reason. Full Python and JS suites pass.

## 8. Downstream

eksenova installs eks-harness from the `main` branch and uses `eks_harness.flows`, the legacy video API and
`studio.publish`. Merging this branch before phase 2 breaks it; eksenova pins its current eks-harness revision first
(a phase 2 step).
