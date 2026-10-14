export const meta = {
  name: 'eks-m0-foundations',
  description: 'Milestone M0 of the eks unified DSL: contracts, eks package, IR, Time/Target/Ref, tracing, registry, all op authoring surfaces, validator, plan CLI, schema, stubs',
  phases: [
    { title: 'Contracts', detail: 'architect writes contracts + typed skeleton + work packages' },
    { title: 'Implement', detail: 'waves of package implementers, each reviewed by two lenses and fixed' },
    { title: 'Integrate', detail: 'full test suite, ruff, pyright, cross-package fixes' },
    { title: 'Complete', detail: 'every design-doc example builds and validates; critic loop' },
  ],
}

const WT = '/Users/esozbek/repos/eksenova/eks-harness/.claude/worktrees/unified-dsl'
const RULES = `
Context and rules (binding):
- Work ONLY inside the git worktree ${WT} (branch feature/unified-dsl of the eks-harness repo). Use absolute paths. Your shell cwd starts elsewhere; always cd into ${WT} in each Bash call.
- The agreed design is ${WT}/docs/dsl/design.md. Read it fully (in ranges of <=300 lines; a hook blocks reading files over 350 lines at once, so always use offset/limit on large files). It is the source of truth; do not deviate from its decisions. Where it is silent, choose the cleanest modern option and record the choice in ${WT}/docs/dsl/decisions-m0.md (append a row: topic | decision | reason).
- M0 contract doc (once written by the architect): ${WT}/docs/dsl/contracts-m0.md. Follow it exactly; if a contract is genuinely wrong, fix it in both the doc and the code and say so in your result.
- Python 3.13+, pydantic v2, full type hints, code must pass ruff and pyright (basic) cleanly. Tests use pytest under ${WT}/tests/eks/.
- NEVER write code comments or docstrings in non-test code (no '#' explanations, no docstrings). Write self-explanatory code. Human descriptions that the op catalog/schema needs go in explicit description= fields/arguments, not docstrings. Comments are allowed in tests.
- Never use the em dash character (U+2014) anywhere (code, docs, commit text); a hook blocks Bash payloads containing it.
- Never git commit, push, create branches or worktrees. Never delete files you did not create unless the contract says so. Do not touch other packages' files except where your task says so; other agents are editing the same worktree concurrently.
- No stubs, TODOs, NotImplementedError or passthrough placeholders in your final code. Implement fully.
- Run tests with: cd ${WT} && EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider <paths>. Lint: cd ${WT} && uv run ruff check <paths>. Types: cd ${WT} && npx --yes pyright@1.1.390 <paths> (or uvx pyright).
- Milestone M0 scope: the authoring side only (package eks, plan IR, validation, plan CLI, ops catalog CLI, JSON Schema export, type stubs/py.typed). No executors; nothing talks to the hub or devices yet. M0 is done when every example plan in design.md builds and validates.
`

const PKG_SCHEMA = {
  type: 'object',
  properties: {
    packages: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          wave: { type: 'integer' },
          title: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          tests: { type: 'array', items: { type: 'string' } },
          spec: { type: 'string' },
        },
        required: ['id', 'wave', 'title', 'files', 'tests', 'spec'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['packages', 'summary'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          file: { type: 'string' },
          line: { type: 'integer' },
          problem: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['severity', 'file', 'problem', 'fix'],
      },
    },
  },
  required: ['issues'],
}

const DONE_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    summary: { type: 'string' },
    tests_passed: { type: 'integer' },
    tests_failed: { type: 'integer' },
    remaining_problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['ok', 'summary', 'remaining_problems'],
}

const CRITIC_SCHEMA = {
  type: 'object',
  properties: {
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          area: { type: 'string' },
          problem: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['area', 'problem', 'fix'],
      },
    },
  },
  required: ['gaps'],
}

phase('Contracts')
const plan = await agent(`${RULES}
You are the M0 architect. Study design.md end to end, and survey the existing code you must align with: ${WT}/src/eks_harness/video/ir (effects, transitions, easings, curves, markers catalogs to carry over into the registry), ${WT}/src/eks_harness/score (timeref grammar, model), ${WT}/src/eks_harness/annotate/spec.py (overlay vocabulary), ${WT}/src/eks_harness/flows and ${WT}/workers/web-driver/src/server.mjs + ${WT}/workers/mobile-driver/src/server.mjs (driver op vocabulary and options), ${WT}/src/eks_harness/plugins (manifest, host, isolation, sdk), ${WT}/src/eks_harness/cli/__init__.py (command registration).

Deliverables:
1. ${WT}/docs/dsl/contracts-m0.md: the precise M0 contracts. Module layout of package eks (src/eks) and eks_harness.ir; every public class/function signature; the Time tick math and full grammar (BNF) incl. offsets, TimeSet, spans, bars with time signatures; the Target model, shorthand grammar and per-platform capability tables; Ref encoding ($ref with path steps incl. find/first/len, $fmt via a format sentinel), typing of Ref[T]; tracing context rules (stable node ids from source position and call path, lanes, program order, eks.when/otherwise/each/parallel/dynamic encoding); Plan IR pydantic models with Merkle hashing (canonical JSON, sha256) and the op-version field; the registry API (op, macro, resource, check, effect, transition, easing, curve, renderer, overlay_kind, target_kind, backend, backend_policy, preset) and how plugins register (thin harness-plugin.toml listing python modules); decorators flow/seed/edit/scene/score and their parameters; each op family's builder surface and arg models (ui, web, mobile, api, net, inject, capture/record, overlay, check/expect, fake, debug, data/resources, edit/fx/tx, gfx, audio, blender scene graph, web scene, score/take incl. at= pinning and hold/pace); the validator rules; CLI commands for M0 (eks-harness plan <file>[::name] [--json] [--set k=v], eks-harness ops [name] [--json], eks-harness schema [--out]); JSON Schema export; py.typed and how the eks package is added to the wheel (pyproject packages) and requires-python >=3.13.
2. A typed skeleton: create the module files with real signatures and data model definitions (pydantic models and dataclasses fully defined; function bodies may temporarily raise NotImplementedError ONLY in this skeleton step; implementers will replace them). Update pyproject.toml (packages, requires-python>=3.13) and register the new CLI command modules in src/eks_harness/cli/__init__.py COMMAND_MODULES with skeleton modules.
3. Split the implementation into work packages that own DISJOINT file sets, assigned to dependency waves: wave 1 = no internal deps (e.g. time, target, ir models+hashing), wave 2 = depends on wave 1 (e.g. ref + tracing + control flow, registry + decorators + discovery), wave 3 = op family surfaces (split into 4 to 6 packages by family) plus validator, plus CLI/schema/stubs. Each package spec must be self-contained: exact responsibilities, files, the contracts section to follow, and the tests it must write (concrete test cases including determinism: same source -> identical plan hash; ids stable under unrelated edits). Aim for 9 to 13 packages.
Return the packages.`, { label: 'architect', phase: 'Contracts', schema: PKG_SCHEMA, model: 'opus', effort: 'max' })

if (!plan || !plan.packages || !plan.packages.length) {
  return { error: 'architect returned no packages' }
}
log(`architect: ${plan.packages.length} packages; ${plan.summary}`)

const waves = [...new Set(plan.packages.map(p => p.wave))].sort((a, b) => a - b)
const results = []

async function reviewAndFix(pkg, label) {
  for (let round = 1; round <= 3; round++) {
    const lenses = [
      'design and contract conformance: does it implement exactly what design.md and contracts-m0.md specify for this package, completely, with no placeholders; are determinism rules honoured (stable ids, canonical hashing, no wall clock or randomness); are public names and parameter shapes consistent with the rest of the eks API (Time everywhere for durations, Target for anchors)',
      'code quality and test strength: correctness bugs, edge cases (grammar corner cases, unicode/Turkish text, empty inputs, overflow), typing holes (Any leaks, Ref typing), pydantic model strictness (extra=forbid), error messages that name the source line, tests that actually assert behaviour (run them), ruff and pyright cleanliness, and the no-comments/no-docstrings rule in non-test code',
    ]
    const reviews = await parallel(lenses.map((lens, i) => () => agent(`${RULES}
Review work package "${pkg.id}" (${pkg.title}). Files: ${pkg.files.join(', ')}. Tests: ${pkg.tests.join(', ')}.
Spec: ${pkg.spec}
Lens: ${lens}.
Read the code and tests, run the package tests, and report only real, specific issues with a concrete fix. Do not edit files. Return an empty list if it is clean.`, { label: `review:${pkg.id}:${i + 1}:r${round}`, phase: 'Implement', schema: REVIEW_SCHEMA, model: 'opus', effort: 'high' })))
    const issues = reviews.filter(Boolean).flatMap(r => r.issues).filter(x => x.severity !== 'low')
    if (!issues.length) return { round, clean: true }
    log(`${pkg.id}: round ${round}, ${issues.length} issues to fix`)
    await agent(`${RULES}
Fix these review findings in work package "${pkg.id}" (${pkg.title}). You own: ${pkg.files.join(', ')} and tests ${pkg.tests.join(', ')}.
Spec: ${pkg.spec}
Findings (JSON): ${JSON.stringify(issues)}
Fix each one properly (or, if a finding is wrong, leave the code and explain why in your result). Then run the package tests, ruff and pyright on your files until clean. Return a short summary.`, { label: `fix:${pkg.id}:r${round}`, phase: 'Implement', model: 'opus', effort: 'high' })
  }
  return { round: 3, clean: false }
}

phase('Implement')
for (const w of waves) {
  const pkgs = plan.packages.filter(p => p.wave === w)
  log(`wave ${w}: ${pkgs.map(p => p.id).join(', ')}`)
  const out = await pipeline(pkgs,
    pkg => agent(`${RULES}
Implement work package "${pkg.id}" (${pkg.title}) for wave ${w}.
You own these files: ${pkg.files.join(', ')}; and these tests: ${pkg.tests.join(', ')}.
Spec: ${pkg.spec}
Read contracts-m0.md and the relevant design.md sections first. Replace every skeleton NotImplementedError in your files with a complete implementation. Earlier waves are already implemented; build on them, do not rewrite them (if you find a bug in an earlier package, fix it minimally and report it). Write thorough tests. Iterate until your tests, ruff and pyright are clean for your files. Return a summary of what you built, any contract changes, and test counts.`, { label: `impl:${pkg.id}`, phase: 'Implement', model: 'opus', effort: 'high' }),
    (impl, pkg) => reviewAndFix(pkg).then(r => ({ id: pkg.id, impl, review: r })),
  )
  results.push(...out.filter(Boolean))
}

phase('Integrate')
let integ = null
for (let i = 1; i <= 3; i++) {
  integ = await agent(`${RULES}
Integration pass ${i} for M0. Run: the eks tests (tests/eks), the whole existing suite (EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider, about 5 minutes; baseline was 1371 passed, 6 skipped), ruff check src/eks src/eks_harness/ir and the new CLI modules, pyright on src/eks and src/eks_harness/ir. Search src/eks and src/eks_harness/ir for leftover NotImplementedError, TODO, pass-only bodies, comments and docstrings in non-test code, and the em dash; fix all. Fix cross-package inconsistencies (naming, Time/Target usage, Ref typing, registry wiring). Verify 'uv build' or at least that the wheel config includes the eks package and py.typed. Verify 'uv run eks-harness ops', 'uv run eks-harness schema --out <tmp>', and 'uv run eks-harness plan' on a sample file work. Report precisely.`, { label: `integrate:${i}`, phase: 'Integrate', schema: DONE_SCHEMA, model: 'opus', effort: 'high' })
  if (integ && integ.ok && !integ.remaining_problems.length) break
}

phase('Complete')
let gaps = []
for (let round = 1; round <= 4; round++) {
  await agent(`${RULES}
Completeness round ${round}. Make sure EVERY code example in design.md (sections 4 to 12, including the flow, control flow, Time, Target, session surface table, seed, network, debug, overlay, edit, scene, score/take, extensibility examples) exists as a real, runnable example file under ${WT}/examples/dsl/ (fix only what is not valid Python in the doc, e.g. use t.span), and that tests/eks/test_design_examples.py builds each one into a Plan and validates it (eks.plan + validator), asserts stable hashes across two builds, and round-trips the Plan through JSON. Implement whatever API surface is missing to make that true, in the package that owns it. Previously found gaps to close: ${JSON.stringify(gaps)}. Run all eks tests, ruff and pyright until clean.`, { label: `complete:${round}`, phase: 'Complete', model: 'opus', effort: 'high' })
  const critic = await agent(`${RULES}
You are the M0 completeness critic. Compare design.md (all sections relevant to M0 authoring: sections 2, 4, 5.1, 6 rules expressible at plan time, 7, 8, 9, 10, 11, 12.1 for plan/ops/schema, 13 check declarations, 15 for stub/schema needs, 16 M0 row) and contracts-m0.md against the actual code in src/eks, src/eks_harness/ir and tests/eks. Run the tests. List every real gap: missing ops or parameters from the session surface table, missing decorators, untyped or Any-typed surfaces, determinism holes at plan time, missing validation rules, examples not covered, placeholders, comments in non-test code. Only list concrete, verifiable gaps. Do not edit.`, { label: `critic:${round}`, phase: 'Complete', schema: CRITIC_SCHEMA, model: 'opus', effort: 'high' })
  gaps = critic ? critic.gaps : []
  log(`critic round ${round}: ${gaps.length} gaps`)
  if (!gaps.length) break
}

const final = await agent(`${RULES}
Final M0 verification. Run the full suite (EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider), ruff, pyright on src/eks and src/eks_harness/ir, and the CLI smoke checks (ops, schema, plan on examples/dsl). Then write ${WT}/docs/dsl/m0-report.md: what was built (modules, public API overview), decisions taken (link decisions-m0.md), test counts, and anything not done. Report precisely and honestly.`, { label: 'final', phase: 'Complete', schema: DONE_SCHEMA, model: 'opus', effort: 'high' })

return { packages: plan.packages.map(p => ({ id: p.id, wave: p.wave, title: p.title })), results, integration: integ, remaining_gaps: gaps, final }
