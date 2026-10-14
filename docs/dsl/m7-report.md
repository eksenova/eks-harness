# M7 report: surfaces and docs

Contract: `contracts-m7.md`. Status: done. The exit check passed on 2026-10-14:
`scripts/check_removed.py` reports no references to removed APIs outside the
allowlist (5 allowlisted as "asserts the tool is gone", 129 as "kept until M5"),
`eks-harness docs --out docs/reference --check` is clean, and the full Python,
JS and typecheck suites pass.

## What landed

| Area | Where | Notes |
| --- | --- | --- |
| MCP sessions | `mcp_engine.py`, `api/routes_sessions_engine.py` | `eks_session_open/do/close/export`; debug run of a one-node `s.debug.break_` flow, pause timeout renewed per op, export lowers ad-hoc ops through `ir/surface.py` |
| Op surface map | `eks_harness/ir/surface.py` | every built-in op to its authoring call, `s.op(name, args)` public; test pins coverage both ways |
| MCP removals | `mcp_*.py`, tests | `driver_act/observe/capture`, `flow_run`, `seed_backend`, `list_personas`, `ensure_backend` gone; absence asserted in tests |
| CLI | `cli/docs_cmds.py`, removed `cli/flow_cmds.py` | new `eks-harness docs [--out DIR] [--check]`; `flow run\|list` removed in favour of `run`, `plan`, `eks_flows` |
| Generated reference | `eks_harness/docs/`, `docs/reference/` | `ops.md`, `catalog.md`, `checks.md`, `presets.md`, `resources.md`, `ir.md`, `cli.md` from registry and JSON Schemas; `--check` test blocks drift |
| Cookbook quotes | `eks_harness/docs/quotes.py`, `docs/cookbook.md` | code quotes synced from `examples/` sources; `stale_quotes` test fails on drift |
| Hand-written docs | `docs/` | new `README.md` index, `cookbook.md`, `determinism.md`, `migration.md`, `overview.md` (replaces `spec.md`), rewritten `drivers.md` for the typed op protocol |
| Frozen app clock | `workers/web-driver/src/server.mjs`, `workers/mobile-driver/src/server.mjs`, `sdk/react-native/src/clock.ts` | plan `now` installed in every browser context (Playwright clock) and via the bridge `clock` command (shifted `Date`); `clock.frozen`, `clock.unsupported`, `clock.failed` events |
| Plugin skills | `claude/skills/eks-*` | `eks-harness`, `eks-dsl`, `eks-flows`, `eks-data`, `eks-network`, `eks-debugging`, `eks-checks`, `eks-edit`, `eks-gfx-audio`, `eks-scenes`, `eks-scores`, `eks-extending`, `eks-mcp`, `eks-migration`; old `flows/web/ios/android/react-native/annotation` skills folded or removed |
| Agents, commands, hooks | `claude/agents`, `claude/commands`, `claude/hooks` | rewritten for `eks-harness run` and `eks_*` tools; hint, evidence and inspection guards read the new output |
| Removal | `api/routes_drivers.py`, `drivers/`, `capture/pace.py`, `capture/steplog.py`, workers `ROUTES`/`EXTENSION_ROUTES`, unused contribution types, `ToolRail.tsx` | kept code is exactly the section 6 take runtime, allowlisted by file and line |

## Decisions taken on the way

* Playwright `^1.55` already ships the `clock` API (added in 1.45), so the web
  worker uses `context.clock.install({ time })` per browser context. `Date`
  starts at `now` and time keeps moving with timers running normally. This keeps
  scheduled UI behaviour intact while dates read deterministically.
* The React Native bridge shifts only `Date` (`clock.ts` keeps `RealDate`,
  applies an offset). `performance.now()` and timers are untouched, so animation
  and scheduling code cannot observe the shift except through wall-clock reads.
* When the app's SDK predates the `clock` command the worker logs
  `clock.unsupported` instead of failing the run. The run stays green and the
  reviewer knows date regions may be noise.
* Native surfaces (date pickers, system UI, status bar) and the backend still
  read their own clocks. `docs/determinism.md` states this as the user contract:
  mask those regions or list them as noise.
* Cookbook quotes are generated, not pasted: `synced()` rewrites each
  `` `examples/...` `` block from its source file, and the reference test fails
  when a quote goes stale. Same policy as the registry-generated reference.

## Exit check

* `uv run python scripts/check_removed.py`: exit 0, "no references to removed
  APIs outside the allowlist".
* `uv run eks-harness docs --out docs/reference --check`: exit 0, "up to date".
* No em dash in the new docs or reference (repo-wide rule).

## Tests

* Python: `pytest -x -q`: 5366 passed, 13 skipped (only warnings: `aifc`/`sunau`
  shims under Python 3.13 in the cookbook audio test).
* JS: `pnpm -r run test`: exit 0 across the workspace (kit, web-driver,
  mobile-driver, react-native, scene suites all green).
* Typechecks: `pnpm -r run typecheck`: exit 0 (tsc for `web`, react-native,
  scene and ui SDKs; `node --check` for the workers).
* Targeted: `tests/test_docs_reference.py` (reference check plus cookbook quote
  sync) and `tests/test_removed_apis.py` pass inside the full run.

## Not done

* M5 and M6 scope stays by design: `score/` with its CLI, routes, MCP tools and
  page; device and score takes on the old `App`/`WorkerSession` runtime
  (`flows/`, `drivers/sessions.py`, `exec`/`call`, `flow_helpers`, worker
  `RUN_ROUTES`, `script-runner.mjs`); Blender and web-scene media plugins; the
  render farm and node jobs; `studio/`; the legacy video API, CLI and
  orchestrator; annotate versions/recapture/project assets; plan diff,
  the recorder and the run page UI.
* Downstream: eksenova still installs eks-harness from `main` and uses
  `eks_harness.flows`, the legacy video API and `studio.publish`. Merging this
  branch before phase 2 breaks it, so eksenova pins its current eks-harness
  revision first (a phase 2 step per contract section 8).
