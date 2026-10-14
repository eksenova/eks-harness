# M1 report: engine and hub

Branch `feature/unified-dsl`, worktree `.claude/worktrees/unified-dsl`, nothing committed. M1 was built by
workflow packages P1 to P11 (2026-10-12) and finished directly on 2026-10-14: the P11 review, P12, the flow
backend rule, integration and this report.

## What exists

| Area | Where | What it does |
| --- | --- | --- |
| Run model | `engine/model.py`, `engine/errors.py` | runs, node instances, states, report, control and do models |
| Values | `engine/values.py` | Ref resolution at run time, output type checks (BL-006) |
| Debugger | `engine/debugger.py` | breakpoints by node, op glob, label and condition; pause on error; tickets; continue, step, retry, skip, edit, do, abort |
| Journal and events | `engine/journal.py`, `db/migrations/0007_engine.sql`, `engine/events.py` | SQLite runs, node instances, expansions, resources and events; bus publishing; SSE with replay from `Last-Event-ID` |
| Report | `engine/report.py` | report, journal pages, run comparison |
| Executors | `engine/executors.py`, `engine/drivers.py`, `ops/` | registry with location and version; real `api.*`, `session.*`, `expect.*` and worker plumbing; fakes for every other family |
| Cache | `engine/cache.py` | content-addressed pure-node cache; keys with resolutions, content digests, executor and plugin digests, fonts, ML and Blender machine facts, run clock (BL-001 to BL-004, BL-008 to BL-010) |
| Registries | `engine/registries.py` | per-tree registries with plugin digests and generations, no leaks between trees (BL-007) |
| Resources | `engine/resources.py`, `engine/auth.py`, `renderq.py` | driver leases, backend leases and identities (BL-005), seed and cassette hooks, render slots, keepalive, persona auth |
| Sandbox | `engine/sandbox.py`, `engine/sandbox_child.py` | subprocess for `@eks.dynamic` with rlimits, no network, process group cleanup |
| Planning and admission | `engine/build.py`, `engine/admission.py` | hub-side plan building, validation, resource and executor checks |
| Scheduler | `engine/scheduler.py` | lanes, program order, parallel lanes, containers, each and dynamic expansion, retries, timeouts, cache flow, debugger hooks |
| Run manager and HTTP | `engine/runs.py`, `api/routes_{plans,runs,ops,flows}.py`, `api/schemas_engine.py` | `/api/plans`, `/api/runs` (create, list, status, report, plan, journal, events, control, cancel, replay, compare), `/api/ops` (catalog, schema, do), `/api/flows` (list, read, save, delete) with grants |
| CLI | `cli/run_cmds.py` | `run` (follow, report, debugger prompt, `--repeat` with comparison, `--no-wait`), `runs list/show/replay/control/cancel`, `debug`, `do` |
| MCP | `mcp_engine.py` | `eks_run`, `eks_plan`, `eks_validate`, `eks_ops`, `eks_do`, `eks_run_status`, `eks_run_control`, `eks_flows`, `eks_flow_get`, `eks_flow_save`, `eks_flow_delete`; errors carry issue codes and source lines |

Size: about 18,900 lines of engine, API, CLI and MCP source and 17,400 lines of engine tests.

## Changes made while finishing

* Flows hold a backend only when they need one (user request, 2026-10-14): a session declares its backend when it
  names `seed=`, `backend=` or `as_`, or on the first op that uses the backend role (`api.*`, `session.as`,
  `backend.info`, `data.*`). `score.take` needs only a lease. A UI-only flow now plans and runs with just its lease.
  Contract: contracts-m0.md session resources; decision row "Backend only when needed".
* Exit codes: aborted runs exit 13 (11 already meant nothing selected), paused runs without a terminal 12.
* `RunCreate.repeat_of` added so `--repeat` links its runs.
* A sandbox test raced on loaded machines (1 s limit before the straggler pid was written); it now starts the
  straggler first with a 3 s limit.

## Verification

* `tests/eks` and `tests/engine`: 3,727 passed, 1 skipped.
* Full suite: 5,098 passed, 7 skipped, 0 failed (8 min 17 s).
* ruff and pyright 1.1.390: clean on `src/eks`, `src/eks_harness/ir`, `src/eks_harness/engine` and the changed API,
  CLI and MCP modules.
* Live hub (worktree daemon on a free port, temporary home, fake pools): `run` streamed node outcomes and passed a
  UI-only flow with no backend in the project; `do` passed; a run paused at `--break op:ui.nav` exited 12 with the
  attach hint; `runs control status` showed the pause with args and snapshots; `runs control continue` resumed it
  to passed; `runs list` and `runs show` matched.

## Backlog

BL-001 to BL-010 (M1) are implemented with the tests named in contracts-m1.md section 22. BL-011 (native dialogs)
is M2. No new open M1 items.

## Not done in M1 (by design)

Real driver executors (web, mobile, React Native) and typed worker ops are M2; real seed execution M3; media,
overlay renderers, gfx and audio M4; scenes and scores M5; recorder, plan debugger UI, plan diff, visual
regression, perf budgets and exploration M6; final MCP, skills, docs and removal of the old APIs M7.
