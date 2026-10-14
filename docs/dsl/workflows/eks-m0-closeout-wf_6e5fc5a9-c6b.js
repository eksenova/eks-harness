export const meta = {
  name: 'eks-m0-closeout',
  description: 'Close the 12 remaining M0 gaps of the eks DSL with file-disjoint fix groups, adversarial verification and a strict critic loop',
  phases: [
    { title: 'Group', detail: 'cluster gaps into file-disjoint fix groups' },
    { title: 'Fix', detail: 'fix each group, two skeptics try to prove each gap still open' },
    { title: 'Recheck', detail: 'strict critic loop for critical/high gaps only' },
    { title: 'Verify', detail: 'full suite, lint, types, report update' },
  ],
}

const WT = '/Users/esozbek/repos/eksenova/eks-harness/.claude/worktrees/unified-dsl'
const GAPS = args.gaps
const RULES = `
Context and rules (binding):
- Work ONLY inside the git worktree ${WT} (branch feature/unified-dsl of eks-harness). Use absolute paths; cd into ${WT} in every Bash call.
- Source of truth: ${WT}/docs/dsl/design.md, ${WT}/docs/dsl/contracts-m0.md, ${WT}/docs/dsl/decisions-m0.md, ${WT}/docs/dsl/m0-report.md. Read large files in ranges of <=300 lines (a hook blocks reading files over 350 lines at once).
- M0 = authoring side only (package eks, plan IR, hashing, validation, plan/ops/schema CLI, JSON Schema, stubs). No executors.
- Python 3.13, pydantic v2, full typing; ruff and pyright 1.1.390 (cd ${WT} && npx --yes pyright@1.1.390 <paths>) must stay clean. Tests under ${WT}/tests/eks: cd ${WT} && EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider tests/eks.
- NEVER write comments or docstrings in non-test code (this includes '# noqa' style suppressions in src; fix the cause instead). Descriptions go in explicit description= fields. Never use the em dash character (U+2014).
- Never git commit, push, branch or create worktrees. Other agents edit the same worktree concurrently: touch only files your task names, plus tests you add.
- No stubs, TODOs or placeholders. Record any new decision as a row in decisions-m0.md and any contract change in contracts-m0.md.
`

const GROUP_SCHEMA = {
  type: 'object',
  properties: {
    groups: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          gap_indexes: { type: 'array', items: { type: 'integer' } },
          files: { type: 'array', items: { type: 'string' } },
          plan: { type: 'string' },
        },
        required: ['id', 'gap_indexes', 'files', 'plan'],
      },
    },
  },
  required: ['groups'],
}
const VERDICT = {
  type: 'object',
  properties: { still_open: { type: 'boolean' }, evidence: { type: 'string' } },
  required: ['still_open', 'evidence'],
}
const CRITIC = {
  type: 'object',
  properties: {
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          area: { type: 'string' },
          problem: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['severity', 'area', 'problem', 'fix'],
      },
    },
  },
  required: ['gaps'],
}
const DONE = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    summary: { type: 'string' },
    remaining_problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['ok', 'summary', 'remaining_problems'],
}

phase('Group')
const grouping = await agent(`${RULES}
These are the remaining M0 gaps (JSON array, index = position): ${JSON.stringify(GAPS)}
Read the code each gap touches. Partition the gaps into fix groups such that no two groups need to edit the same source file (put gaps sharing a file, e.g. eks_harness/ir/hashing.py, into one group). For each group list every file it will edit and a concrete fix plan. Do not edit anything.`, { label: 'group', phase: 'Group', schema: GROUP_SCHEMA, model: 'opus', effort: 'high' })

const groups = grouping.groups
log(`${groups.length} fix groups: ${groups.map(g => g.id + '[' + g.gap_indexes.join(',') + ']').join(' ')}`)

async function fixGroup(g) {
  let open = g.gap_indexes.map(i => ({ index: i, gap: GAPS[i], evidence: '' }))
  for (let round = 1; round <= 3 && open.length; round++) {
    await agent(`${RULES}
Fix group ${g.id}, round ${round}. You own these files: ${g.files.join(', ')} (plus new tests in tests/eks).
Plan: ${g.plan}
Gaps to close (with any evidence from skeptics that they are still open): ${JSON.stringify(open)}
Close every gap fully and add tests that would fail without the fix (including the exact reproduction each gap describes). Run tests/eks, ruff and pyright on your files until clean. Return a summary.`, { label: `fix:${g.id}:r${round}`, phase: 'Fix', model: 'opus', effort: 'high' })
    const checks = await parallel(open.map(o => () => parallel([1, 2].map(k => () => agent(`${RULES}
You are a skeptic. Try to show that this M0 gap is STILL OPEN after the latest fix: ${JSON.stringify(o.gap)}
Reproduce the original problem with a scratch script under /Users/esozbek/.claude/jobs/a66c1db0/tmp/ (not in the repo), read the relevant code and tests, and try at least one variant of the scenario the fix may have missed. Do not edit repo files. still_open=true only with concrete evidence (command and output).`, { label: `skeptic:${g.id}:${o.index}:${k}:r${round}`, phase: 'Fix', schema: VERDICT, model: 'opus', effort: 'high' })))
      .then(vs => ({ ...o, votes: vs.filter(Boolean) }))))
    open = checks.filter(c => c.votes.some(v => v.still_open)).map(c => ({ index: c.index, gap: c.gap, evidence: c.votes.filter(v => v.still_open).map(v => v.evidence).join('\n---\n') }))
    log(`${g.id} round ${round}: ${open.length} still open`)
  }
  return { id: g.id, still_open: open }
}

phase('Fix')
const fixed = await pipeline(groups, g => fixGroup(g))

phase('Recheck')
const seen = new Set()
const recheck = []
let dry = 0
for (let round = 1; round <= 4 && dry < 1; round++) {
  const critic = await agent(`${RULES}
You are a strict M0 critic. Compare design.md (authoring-relevant sections), contracts-m0.md and decisions-m0.md with the code in src/eks, src/eks_harness/ir, src/eks_harness/engine and tests/eks. Run the tests. Report only CRITICAL or HIGH gaps: determinism holes (hashes, seeds, clock, cache-key inputs), incorrect behaviour, contract violations, missing design-mandated authoring features, broken validation. Prove each with a scratch reproduction under /Users/esozbek/.claude/jobs/a66c1db0/tmp/. Already handled (do not report again): ${JSON.stringify([...seen])}. Do not edit repo files.`, { label: `critic:r${round}`, phase: 'Recheck', schema: CRITIC, model: 'opus', effort: 'max' })
  const fresh = (critic ? critic.gaps : []).filter(x => (x.severity === 'critical' || x.severity === 'high') && !seen.has(x.area))
  log(`critic round ${round}: ${fresh.length} new critical/high gaps`)
  if (!fresh.length) { dry++; continue }
  fresh.forEach(x => seen.add(x.area))
  await agent(`${RULES}
Close these critical/high M0 gaps found by the critic: ${JSON.stringify(fresh)}
You may edit any file in src/eks, src/eks_harness/ir, src/eks_harness/engine, the plan/ops/schema CLI modules and tests/eks needed, but check git diff first and keep other recent changes intact. Add regression tests reproducing each gap. Run tests/eks, ruff and pyright until clean.`, { label: `refix:r${round}`, phase: 'Recheck', model: 'opus', effort: 'high' })
  recheck.push(...fresh)
}

phase('Verify')
const final = await agent(`${RULES}
Final M0 closeout verification. Run the full suite (EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider), ruff and pyright on src/eks, src/eks_harness/ir, src/eks_harness/engine and the three CLI modules, pyright on tests/eks, and the plan CLI on every file under examples/dsl/.harness (use --platform/--set where needed). Remove '# noqa' suppressions from examples/dsl by fixing their causes. Delete tests/eks/.ruff_cache if present. Scan non-test code for comments, docstrings, TODO, NotImplementedError and the em dash. Then update docs/dsl/m0-report.md with a 'Closeout' section: gaps closed, anything still open, final counts. Report honestly.`, { label: 'final', phase: 'Verify', schema: DONE, model: 'opus', effort: 'high' })

return { groups: groups.map(g => ({ id: g.id, gaps: g.gap_indexes })), fixed, recheck: recheck.map(x => x.area), final }
