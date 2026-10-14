export const meta = {
  name: 'eks-m1-engine-hub',
  description: 'M0 residual triage, then milestone M1 of the eks DSL: engine and hub (plans/runs API, scheduler, journal, cache, resource planner, executors, run CLI, core MCP)',
  phases: [
    { title: 'Residuals', detail: 'skeptics re-check open M0 items; fix authoring ones, route engine ones into M1' },
    { title: 'Contracts', detail: 'architect writes contracts-m1.md, skeleton and work packages' },
    { title: 'Implement', detail: 'waves of implementers with two-lens review and fixes' },
    { title: 'Integrate', detail: 'full suite, lint, types, end-to-end run' },
    { title: 'Complete', detail: 'capped critic loop; leftovers to backlog.md' },
  ],
}

const WT = '/Users/esozbek/repos/eksenova/eks-harness/.claude/worktrees/unified-dsl'
const RES = args.residuals
const RULES = `
Context and rules (binding):
- Work ONLY inside the git worktree ${WT} (branch feature/unified-dsl of eks-harness). Absolute paths; cd into ${WT} in every Bash call.
- Sources of truth: ${WT}/docs/dsl/design.md (the agreed design), contracts-m0.md, decisions-m0.md, m0-report.md, and (once written) contracts-m1.md and decisions-m1.md, backlog.md, all in ${WT}/docs/dsl. Read large files in ranges of <=300 lines (a hook blocks reading files over 350 lines at once).
- M0 (authoring: package eks, plan IR, hashing, validator, plan/ops/schema CLI) is done. M1 scope per design.md section 16: the engine and hub. POST /api/plans (validate, plan, diff later), /api/runs (start, status, events stream, control, journal), /api/ops; scheduler over lanes with program order and parallel independent lanes; executors registry with location; SQLite journal (runs, run_nodes, node_events) via a new db migration; content-addressed cache for pure nodes with correct cache keys (design 6); resource planner that acquires driver leases, backend leases (the existing bl_ lease API), seed snapshot restore hooks (seeding itself is M3), cassettes hooks, render node slots; dynamic sandbox subprocess for @eks.dynamic (rlimits, no network); debugger core in the engine (breakpoints by node id, op, label, condition; pause on error; continue/step/retry/skip/abort/edit-args); run report model; CLI eks-harness run/runs/do; core MCP tools (eks_run, eks_plan, eks_validate, eks_ops, eks_do, eks_run_status, eks_run_control, eks_flows/eks_flow_get/eks_flow_save/eks_flow_delete); executors for api.* (real HTTP against the backend lease URL with persona auth hook), session.*, ctl.*, store/evidence bookkeeping, and FAKE executors for every other op family so a full plan runs end to end in tests. Real driver, seed and media executors are M2 to M5. Old APIs keep working until M7.
- Existing platform code to integrate with, not duplicate: src/eks_harness/daemon (leases.py, app.py), pools (base.py BackendManager and backend leases), db (migrations), api routes and schemas, store, nodes, renderq.py, mcp_server.py/mcp_system.py, cli.
- Python 3.13, pydantic v2, full typing. ruff and pyright 1.1.390 (cd ${WT} && npx --yes pyright@1.1.390 <paths>) clean on new code. Tests: cd ${WT} && EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider <paths>; new tests under ${WT}/tests/engine/.
- NEVER write comments, docstrings or '# noqa' in non-test code. Descriptions go in description= fields. Never use the em dash (U+2014).
- Never git commit, push, branch or create worktrees. Other agents edit the same worktree concurrently: touch only files your task names plus your tests.
- No stubs, TODOs, NotImplementedError or placeholders in final code.
`

const VERDICT = { type: 'object', properties: { still_open: { type: 'boolean' }, kind: { type: 'string', enum: ['authoring', 'engine', 'later'] }, evidence: { type: 'string' } }, required: ['still_open', 'kind', 'evidence'] }
const PKG_SCHEMA = { type: 'object', properties: { packages: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, wave: { type: 'integer' }, title: { type: 'string' }, files: { type: 'array', items: { type: 'string' } }, tests: { type: 'array', items: { type: 'string' } }, spec: { type: 'string' } }, required: ['id', 'wave', 'title', 'files', 'tests', 'spec'] } }, summary: { type: 'string' } }, required: ['packages', 'summary'] }
const REVIEW_SCHEMA = { type: 'object', properties: { issues: { type: 'array', items: { type: 'object', properties: { severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, file: { type: 'string' }, line: { type: 'integer' }, problem: { type: 'string' }, fix: { type: 'string' } }, required: ['severity', 'file', 'problem', 'fix'] } } }, required: ['issues'] }
const DONE = { type: 'object', properties: { ok: { type: 'boolean' }, summary: { type: 'string' }, tests_passed: { type: 'integer' }, tests_failed: { type: 'integer' }, remaining_problems: { type: 'array', items: { type: 'string' } } }, required: ['ok', 'summary', 'remaining_problems'] }
const CRITIC = { type: 'object', properties: { gaps: { type: 'array', items: { type: 'object', properties: { severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, area: { type: 'string' }, problem: { type: 'string' }, fix: { type: 'string' } }, required: ['severity', 'area', 'problem', 'fix'] } } }, required: ['gaps'] }

phase('Residuals')
const triaged = await parallel(RES.map((item, i) => () => agent(`${RULES}
Re-check this open item left from M0: ${JSON.stringify(item)}
Reproduce it with a scratch script under /Users/esozbek/.claude/jobs/a66c1db0/tmp/m1res/ (not in the repo). Decide: still_open (with concrete evidence) and kind: 'authoring' if it is a plan-time problem in src/eks, src/eks_harness/ir or the validator; 'engine' if it is about cache keys, machine dependence, artifact immutability, registry scoping in a long-running host, session/lease identity at run time or anything the M1 engine must enforce; 'later' if it belongs to M2 to M7 (say which). Do not edit repo files.`, { label: `residual:${i}`, phase: 'Residuals', schema: VERDICT, model: 'opus', effort: 'high' }).then(v => v ? { item, ...v } : null)))
const open = triaged.filter(Boolean).filter(x => x.still_open)
const authoring = open.filter(x => x.kind === 'authoring')
const engineItems = open.filter(x => x.kind === 'engine')
const laterItems = open.filter(x => x.kind === 'later')
log(`residuals: ${open.length} open (${authoring.length} authoring, ${engineItems.length} engine, ${laterItems.length} later)`)

if (authoring.length) {
  await agent(`${RULES}
Fix these confirmed plan-time (authoring) defects left from M0, each with a regression test reproducing the evidence: ${JSON.stringify(authoring)}
Check git diff first; keep other work intact. Run tests/eks, ruff and pyright on touched files until clean. Record decisions in decisions-m0.md.`, { label: 'residual-fix', phase: 'Residuals', model: 'opus', effort: 'high' })
}
await agent(`${RULES}
Create ${WT}/docs/dsl/backlog.md: a table (id | milestone | severity | area | problem | evidence/fix) of open items. Add every item below with milestone M1 for engine items and the named milestone for later items. Engine items: ${JSON.stringify(engineItems)} Later items: ${JSON.stringify(laterItems)}`, { label: 'backlog', phase: 'Residuals', model: 'opus', effort: 'medium' })

phase('Contracts')
const plan = await agent(`${RULES}
You are the M1 architect. Study design.md (sections 3, 5, 6, 9, 12, 16), contracts-m0.md, decisions-m0.md, docs/dsl/backlog.md (every M1 row is a requirement for M1), and the existing platform code you integrate with (daemon/leases.py, daemon/app.py, pools/base.py BackendManager and backend leases, db/migrations, api routes and schemas, store, nodes, renderq.py, mcp_server.py, mcp_system.py, cli/__init__.py, and src/eks_harness/engine from M0).
Deliverables:
1. ${WT}/docs/dsl/contracts-m1.md: exact contracts for the engine package layout; executor protocol (op -> executor, location hub|worker|node|blender|browser-scene, idempotency, purity, version), run lifecycle and states, scheduler semantics (lanes, program order, parallel lanes, after edges, Ref resolution, when/each/dynamic expansion at run time, journaled sub-plans), journal schema (SQL migration), cache (keys including every design-6 input and backlog M1 items, storage, invalidation), resource planner (driver leases, backend leases, seed snapshot hook, cassette hook, render slots, release), dynamic sandbox protocol, debugger state machine and control API, events (bus + SSE), run report model, HTTP API (/api/plans, /api/runs, /api/ops) with schemas, auth/grants reuse, CLI (run, runs list/show/replay, do) and MCP tools (eks_run, eks_plan, eks_validate, eks_ops, eks_do, eks_run_status, eks_run_control, eks_flows, eks_flow_get, eks_flow_save, eks_flow_delete), fake executors for tests, the api.* real executor, and how every backlog M1 item is satisfied.
2. A typed skeleton (signatures and models; NotImplementedError allowed only in this step), the db migration file, and route/CLI/MCP registration wiring.
3. Work packages with DISJOINT file sets in dependency waves (8 to 12 packages), each with a self-contained spec and concrete tests (including an end-to-end test: a plan with api ops against a local test HTTP server plus fake executors for ui/check/capture/overlay/edit runs through POST /api/runs to completion with a journal, events, a report and cache hits on a second run).`, { label: 'architect', phase: 'Contracts', schema: PKG_SCHEMA, model: 'opus', effort: 'max' })

if (!plan || !plan.packages || !plan.packages.length) return { error: 'architect returned no packages', open }
log(`architect: ${plan.packages.length} packages`)

async function reviewAndFix(pkg) {
  for (let round = 1; round <= 3; round++) {
    const lenses = [
      'design and contract conformance (design.md, contracts-m1.md, backlog M1 rows): completeness, no placeholders, determinism and cache-key correctness, consistency with the eks API and existing platform code (leases, backend leases, store, auth)',
      'correctness and robustness: concurrency and races (asyncio, threads, SQLite transactions), resource release on failure and cancellation, error model, security (sandbox, auth on routes), tests that really assert behaviour (run them), ruff/pyright cleanliness, no comments/docstrings in non-test code',
    ]
    const reviews = await parallel(lenses.map((lens, i) => () => agent(`${RULES}
Review M1 work package "${pkg.id}" (${pkg.title}). Files: ${pkg.files.join(', ')}. Tests: ${pkg.tests.join(', ')}.
Spec: ${pkg.spec}
Lens: ${lens}. Run the package tests. Report only real, specific issues with concrete fixes. Do not edit.`, { label: `review:${pkg.id}:${i + 1}:r${round}`, phase: 'Implement', schema: REVIEW_SCHEMA, model: 'opus', effort: 'high' })))
    const issues = reviews.filter(Boolean).flatMap(r => r.issues).filter(x => x.severity === 'critical' || x.severity === 'high' || (round === 1 && x.severity === 'medium'))
    if (!issues.length) return { round, clean: true }
    await agent(`${RULES}
Fix these review findings in M1 package "${pkg.id}". You own: ${pkg.files.join(', ')} and tests ${pkg.tests.join(', ')}. Spec: ${pkg.spec}
Findings: ${JSON.stringify(issues)}
Fix properly (or explain why a finding is wrong). Run package tests, ruff, pyright until clean.`, { label: `fix:${pkg.id}:r${round}`, phase: 'Implement', model: 'opus', effort: 'high' })
  }
  return { round: 3, clean: false }
}

phase('Implement')
const waves = [...new Set(plan.packages.map(p => p.wave))].sort((a, b) => a - b)
const results = []
for (const w of waves) {
  const pkgs = plan.packages.filter(p => p.wave === w)
  log(`wave ${w}: ${pkgs.map(p => p.id).join(', ')}`)
  const out = await pipeline(pkgs,
    pkg => agent(`${RULES}
Implement M1 work package "${pkg.id}" (${pkg.title}), wave ${w}. You own: ${pkg.files.join(', ')}; tests: ${pkg.tests.join(', ')}.
Spec: ${pkg.spec}
Read contracts-m1.md and relevant design/backlog sections first. Replace every skeleton NotImplementedError in your files. Build on earlier waves; fix bugs there minimally and report them. Write thorough tests. Iterate until tests, ruff and pyright are clean. Return a summary with test counts and any contract changes.`, { label: `impl:${pkg.id}`, phase: 'Implement', model: 'opus', effort: 'high' }),
    (impl, pkg) => reviewAndFix(pkg).then(r => ({ id: pkg.id, review: r })),
  )
  results.push(...out.filter(Boolean))
}

phase('Integrate')
let integ = null
for (let i = 1; i <= 3; i++) {
  integ = await agent(`${RULES}
M1 integration pass ${i}. Run the full suite (EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider; last known 4326 passed, 6 skipped), ruff and pyright on all M1 and M0 source, and these live checks against a real daemon started from the worktree on a free port with a temporary EKS_HARNESS_HOME (never the user's real hub on 7171): eks-harness run on an example flow using fake executors (document how fakes are selected), eks-harness runs list/show, eks-harness do api.get against a local test server, the MCP tools via the in-process MCP test client. Fix cross-package problems, leftover placeholders, comments and em dashes. Stop the temporary daemon afterwards. Report precisely.`, { label: `integrate:${i}`, phase: 'Integrate', schema: DONE, model: 'opus', effort: 'high' })
  if (integ && integ.ok && !integ.remaining_problems.length) break
}

phase('Complete')
const seen = new Set()
const leftovers = []
for (let round = 1; round <= 3; round++) {
  const critic = await agent(`${RULES}
You are the M1 completeness critic. Compare design.md (M1-relevant sections), contracts-m1.md, backlog.md M1 rows and the code. Run tests and prove each gap with a scratch reproduction under /Users/esozbek/.claude/jobs/a66c1db0/tmp/m1critic/. Report only critical/high gaps: missing M1 features, wrong behaviour, determinism or cache-key holes, races, resource leaks, security holes. Already handled: ${JSON.stringify([...seen])}. Do not edit.`, { label: `critic:r${round}`, phase: 'Complete', schema: CRITIC, model: 'opus', effort: 'max' })
  const fresh = (critic ? critic.gaps : []).filter(x => (x.severity === 'critical' || x.severity === 'high') && !seen.has(x.area))
  log(`critic round ${round}: ${fresh.length} new`)
  if (!fresh.length) break
  fresh.forEach(x => seen.add(x.area))
  const fixedOk = await agent(`${RULES}
Close these M1 gaps, each with a regression test: ${JSON.stringify(fresh)}
Check git diff first and keep other work intact. Run the engine and eks tests, ruff and pyright until clean. Return JSON-like text listing which gaps you closed and which you could not.`, { label: `refix:r${round}`, phase: 'Complete', model: 'opus', effort: 'high' })
  const verify = await parallel(fresh.map((g, i) => () => agent(`${RULES}
Skeptic: try to show this M1 gap is still open after the fix: ${JSON.stringify(g)}. Scratch scripts under /Users/esozbek/.claude/jobs/a66c1db0/tmp/m1critic/. Do not edit repo files.`, { label: `verify:r${round}:${i}`, phase: 'Complete', schema: VERDICT, model: 'opus', effort: 'high' }).then(v => ({ g, v }))))
  leftovers.push(...verify.filter(x => x && x.v && x.v.still_open).map(x => ({ ...x.g, evidence: x.v.evidence })))
}

const final = await agent(`${RULES}
Final M1 verification. Run the full suite, ruff and pyright on M0 and M1 source and tests, and the live daemon checks (temporary home, free port; never touch the real hub). Append these unresolved items to docs/dsl/backlog.md with milestone M1 and severity: ${JSON.stringify(leftovers)}. Write docs/dsl/m1-report.md: what was built (engine modules, API, CLI, MCP), how the backlog M1 rows were satisfied or not, test counts, open items. Report honestly.`, { label: 'final', phase: 'Complete', schema: DONE, model: 'opus', effort: 'high' })

return { residuals: { open: open.length, authoring: authoring.length, engine: engineItems.length, later: laterItems.length }, packages: plan.packages.map(p => ({ id: p.id, wave: p.wave, title: p.title })), results, integration: integ, critic_seen: [...seen], leftovers: leftovers.map(x => x.area), final }
