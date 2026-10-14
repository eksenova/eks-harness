---
name: eks-dsl
description: "Core concepts of the eks authoring API: entry decorators, plans and nodes, Refs (lazy values, paths, find/filter, f-strings), Time (the one grammar for seconds, frames, beats, bars, cues, markers and words), Targets (by.*, shorthand strings, within/near/nth, the native surface), control flow (eks.when, eks.otherwise, eks.each, eks.parallel, @eks.dynamic) and the determinism rules. Use when writing or reading any eks flow, seed, edit, scene or score, or when a plan fails to build or validate."
---

# The eks DSL

`import eks` is the whole authoring API. Decorated functions are traced into a plan; nothing runs while
tracing, so code inside them is plan-building code, not test code.

```python
import eks
from eks import by, expect, t


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def refs(s: eks.Session):
    invoices = s.api.get("/api/invoices", query={"MaxResultCount": 20})
    newest = invoices.items.first
    s.ui.nav(f"/tenant/invoice/{newest.id}")
    expect(by.test_id("invoice-number")).text(newest.number)
    expect(invoices["totalCount"]).gt(0)
    return {"drafts": invoices.items.filter(state="Draft"), "first_three": invoices.items[:3]}
```

## Entries

| Decorator | Root object | Folder |
| --- | --- | --- |
| `@eks.flow(app=, seed=, as_=, backend=, platform=, platforms=, viewport=, pace=, timeout=, noise=, checks=, timezone=, locale=, permissions=, now=, root_seed=, tags=)` | `s: eks.Session` | `.harness/flows/`, `<app>/.harness/flows/` |
| `@eks.seed(name, backend=, now=, seed=, base=)` | `d: eks.Data` | `.harness/seeds/` |
| `@eks.edit(fps=, size=, seed=, duration=, window=, background=)` | `e: eks.Edit` | `.harness/edits/` |
| `@eks.scene(kind="gfx"\|"blender"\|"web", size=, fps=, duration=, seed=, ...)` | `sc: eks.GfxScene` / `BlenderScene` / `WebScene` | `.harness/scenes/` |
| `@eks.score(fps=, song=, window=, size=, seed=, bpm=, duration=)` | `sc: eks.Score` | `.harness/scores/` |

Extra function parameters are entry parameters, typed by their annotations: `--set name=value` (CLI) or
`params={"name": "value"}` (MCP) fills them. `eks.plan(fn, **params)` builds a plan in Python;
`eks.validate(plan)` checks it. A file can hold several entries; pick one with `file.py::name`.

```bash
eks-harness plan .harness/flows/refs.py [--json] [--platform ios] [--set k=v]   # exit 0 valid, 1 issues, 2 build error
eks-harness schema --op ui.nav                                                 # JSON Schema of one op
```

## Refs

Every op returns a `Ref` typed by the op's output (`Ref[Json]`, `Ref[str]`, `Ref[int]`, `Ref[bool]`,
`Ref[Artifact]`). A Ref is a path into a node's future output:

- `ref.items`, `ref["totalCount"]`, `ref.items[0]`, `ref.items[:3]` build paths.
- `ref.items.find(name="Kadıköy")` (first match), `.filter(state="Draft")` (all matches), `.first`, `.last`,
  `.len()` (a `Ref[int]`). No lambdas: matching is by equality on fields.
- f-strings carry Refs into any text argument: `s.ui.nav(f"/invoice/{inv.id}")`, `by.test_id(f"row-{inv.id}")`.
- `bool(ref)`, `if ref:` and `<`/`>` raise; `ref == x` compares the Ref object, never its value. Branch with
  `eks.when`, assert with `expect(ref).eq(...)`.
- The entry's return value (a dict of Refs and literals) becomes the run report's `values`.

## Time

One type, integer ticks (705 600 000 per second), one grammar. Units: `ms`, `s`, `tk`, `f` (frames), `b`
(beats), `bar`. There is no minute unit: write `t("600s")`.

```python
t("1.5s"); t("300ms"); t("f:120"); t("8f"); t("beat:8") + t("1b"); t("bar:4"); t("downbeat:2")
t("cue:logo") - t("2f"); t("marker:intro#2"); t("word:'EksenGOLD'"); t.start; t.end; t.end - t("2s")
t.span(t("0s"), t("15s"))                       # a Span
t.beats("downbeat")[0:8]                        # a TimeSet: the first eight downbeats
t.set(t("2s"), t("3s")); t.every(t("1s"), t.span(t("0s"), t("4s")))
```

- `f:120` is the absolute frame 120, `8f` a length of eight frames; the same for `beat:8` and `1b`.
- Beat, bar, cue, marker and word times resolve against the active clock (an edit's or score's song, a
  `bpm=`, cues from `e.cue(...)`, marker analyses). Indices are 0-based.
- Durations (`timeout=`, `hold=`, fades, transitions) must be positive lengths; `t("0s")` and
  `t("beat:8")` are not lengths. A plain number where a `Time` belongs is a type error.

## Targets

```python
by.test_id("credit-add-submit")            # "#credit-add-submit"
by.role("button", name="Sunuyu iptal et")  # "role=button:Sunuyu iptal et"
by.text("Kadıköy"); by.text("Kadı", exact=False)          # a bare string is exact text
by.label("E-posta"); by.placeholder("Ara")
by.css("li[data-package-id='3']")          # web only; "css=..." in shorthand
by.kind("button")                          # React Native node kind
by.point(120, 480); by.rect(40, 1600, 1000, 200)
by.test_id("list").within(by.role("dialog")).nth(2)
by.text("Manuel").near(by.text("Şablon ile"))
by.text("İzin ver").native                 # system UI outside the app (iOS, Android)
```

Shorthand strings: `#id` is a test id, `prefix=rest` picks a kind (`testid`, `role`, `text`, `label`,
`placeholder`, `css`, `kind`, `point`, `rect`, `object`, `layer`, plus plugin shorthands); anything else is
exact text. There is no CSS guessing. Platform support is checked at plan time (a `css` target on iOS is a
plan error). Matching is strict: no match waits until the timeout (`target_not_found`, with near misses), more
than one fails (`target_ambiguous`, with up to 10 candidates) unless `.nth(i)` (`-1` is the last), `.first` or
`.last` picks one.

## Control flow

```python
with eks.when(s.ui.exists("#empty-state")):
    s.ui.tap("#create")
with eks.otherwise():
    s.ui.tap(by.test_id("row").first)

with eks.each(s.api.get("/api/invoices").items[:3]) as inv:   # runtime list: expands when it runs
    s.ui.nav(f"/tenant/invoice/{inv.id}")
    s.checkpoint(f"invoice-{inv.number}")

with eks.parallel():                                          # independent lanes
    s.ui.nav("/tenant/invoice/list")
    s.api.get("/api/invoices")

rows = s.ui.count("#row")

@eks.dynamic                                                  # runs on the hub with real values
def per_row(n=rows):
    return [s.ui.tap(by.test_id("row").nth(i)) for i in range(n)]
```

- `eks.when` takes a `Ref[bool]` or `Ref[Json]`; `eks.each(over, limit=)` takes a Ref or a literal list (a
  literal list expands at plan time).
- `@eks.dynamic` is the escape hatch: its defaults are Refs resolved at run time, it runs in a sandboxed
  subprocess (no network, resource limits) and returns ops that become a sub-plan. Its source hash is part of
  the node hash. It cannot add a backend: a flow that needs one names it (`seed=`, `backend=` or `as_`).
- Plain Python `for` loops over literals are fine: they unroll at plan time.

## Determinism rules

- Waits are conditions (`wait_for`, `expect.eventually`, `settle`), each with a `Time` timeout. There is no
  sleep op; `s.hold(t)` exists only inside a recording, for pacing.
- The clock is frozen per plan (`now=` on the flow or inherited from its seed); web pages get it through
  Playwright's clock, React Native through the bridge.
- One root seed (`root_seed=`, or the seed's `seed=`); every node derives its own seed from it and its path.
  Seed resources derive ids (TCKN, VKN, IBAN, ULID) from their path.
- Node ids come from the source position and call path, so a plan stays stable under blank lines, comments
  and renames; hashes form a Merkle tree, which is what the render cache keys on.
- Cassettes (`s.net.cassette(name, mode="replay")`) make network traffic hermetic.
- `eks-harness run file.py --repeat 3` runs three times and lists the nodes whose results diverge.

Generated reference: `docs/reference/ir.md` (plan IR) and `docs/reference/ops.md` in the eks-harness
repository; `eks-harness ops [name]` for the hub you use.
