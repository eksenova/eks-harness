---
name: eks-migration
description: "Port code written for the earlier eks-harness API to `import eks`: old flow files (def flow(app) with app.goto, app.click, app.shot, recording_of) to @eks.flow plans, app.py helper classes to @eks.app runtimes and @eks.macro, the old flow subcommand to eks-harness run, the per-step MCP driver tools to eks_session_* and eks_do, seeder scenarios and backend personas to @eks.seed and seed personas, target strings and time strings to by.* and t(...), plugin contribution types that became decorators. Use when converting a repository's harness setup, flows or plugin, or when an old example does not run any more."
---

# Migrating to the eks API

The earlier API was imperative: a flow function received an app object and each call ran at once. The new
API builds a plan first and runs it on the hub, so values are Refs, waits are conditions and every time is
a `Time`. Port file by file and run `eks-harness plan` on each one until it validates; the full guide is
`docs/migration.md` in the eks-harness repository.

## Files and commands

| Before | Now |
| --- | --- |
| `<app>/.harness/flows/x-check.py` with `def flow(app):` | the same folder, `@eks.flow(app="<app>") def x_check(s: eks.Session):` |
| the old `flow` subcommand (with `-e` for inline code) | `eks-harness run <file>[::name]`; inline code goes through `eks_run(source=...)` or `eks-harness do <op>` |
| `--release` on a run | gone: every run releases its leases when it ends |
| `--set key=value` | the same, parsed into the entry function's annotated parameters |
| `<app>/.harness/app.py` with an app class and `boot()` | an `@eks.app` runtime in a trusted repo plugin (`prepare`, `boot`, `login`, `close`) plus `@eks.macro` for screen helpers |
| plugin contribution types for flow helpers, logins, fakes, platforms and app profiles | `@eks.macro`, the runtime's `login`, the bridge's `registerFake`, the app profile `app.toml` |
| per-step MCP driver tools (act, observe, capture) | `eks_session_open` / `eks_session_do` / `eks_session_close`, `eks_do` for one op |
| MCP tool that ran a flow file | `eks_run` |
| seeder scenarios, `backend seed --scenario`, the MCP seeding tool | `@eks.seed` with `@eks.resource` kinds, `eks-harness run seeds.py::name` |
| backend personas from credential providers | seed personas (`persona=True` kinds), `eks-harness seed show`, `eks_seed_show` |
| the MCP tool that started a backend for a flow | `backend_acquire`, or let the run acquire it (`seed=`, `backend=`, `as_=`) |

## Calls

| Before (app object) | Now (session) |
| --- | --- |
| `app.goto(path)`, `app.nav(path)` | `s.ui.nav(path)`; `s.ui.open_url(url)` for a full load |
| `app.click(t)`, `app.press(t)` | `s.ui.tap(target)`; `long=` became `s.ui.long_press(target, hold=)` |
| `app.fill(t, v)`, `app.type(t, v)` | `s.ui.fill(target, value)`, `s.ui.type(target, value, pace=)` |
| `app.press(key)` (keyboard) | `s.ui.key("Enter", target=None)` |
| `app.navigate(name, params)`, `app.go_back()` | `s.ui.nav(name, params=)`, `s.ui.back()` |
| `app.text(t)`, `app.exists(t)`, `app.count(t)` returning values | `s.ui.text(...)`, `s.ui.exists(...)`, `s.ui.count(...)` returning Refs; assert with `expect(...)`, branch with `eks.when` |
| `assert app.text("h1") == "Faturalar"` | `expect(by.role("heading")).text("Faturalar")` |
| `if app.exists("#empty"):` | `with eks.when(s.ui.exists("#empty")):` |
| `time.sleep(2)`, fixed waits | `s.ui.wait_for(...)`, `expect.eventually(...)`, `s.ui.settle()`; `s.hold(t("2s"))` only inside a recording |
| `app.js(code, arg)` | `s.eval(source, arg)` |
| `app.api(method, path, body)` | `s.api.get/post/put/patch/delete(path, json=, as_=)` |
| `app.login(...)`, `app.as_(...)` from a plugin | `@eks.flow(as_="persona")` or `s.as_("persona")`, served by the runtime's `login` |
| `app.arm("camera", media=...)`, `app.clear_fakes()` | `s.fake.arm("camera", media=eks.media.source("fixtures/x.jpg"))`, `s.fake.clear()` |
| `app.patch(...)`, `app.inject(component)`, `app.dispatch(action)`, `app.state(path)` | `s.mobile.patch(...)`, `s.mobile.mount(component)`, `s.mobile.dispatch(action)`, `s.mobile.state(path)` |
| `app.shot(name, phone=True)` | `s.shot(name)` or `s.shot(name, viewport="phone")` |
| `app.checkpoint(label)` | `s.checkpoint(label)`; failing checks now fail the run unless `soft=True` |
| `with app.recording_of(name, title=, subtitle=):` | `with s.record(name):` and `s.overlay.title(text, subtitle=, hold=t("2s"))` |
| `app.caption`, `app.callout`, `app.highlight`, `app.spotlight` | `s.overlay.caption/callout/highlight/spotlight(...)` with `hold=` or `keep=` |
| `app.backend.credentials` in a flow | a persona: `as_=` or `s.api.get(..., as_="persona")` |
| `result = {...}` at the end of a flow | `return {...}` from the entry function |
| OS permission prompts granted in `boot()` | `@eks.flow(permissions="grant")`, or tap them with `by.text("Allow").native` |

## Targets and times

| Before | Now |
| --- | --- |
| `"#testId"` | the same, or `by.test_id("testId")` |
| `"text=Kaydet"` (substring) | `by.text("Kaydet", exact=False)`; a bare `"Kaydet"` and `"text=Kaydet"` are exact now |
| `"role=button:Save"`, `"label=..."`, `"placeholder=..."`, `"css=..."` | the same shorthand, or `by.role("button", name="Save")`, `by.label(...)`, `by.placeholder(...)`, `by.css(...)` |
| `{"role": "button", "name": "Save", "nth": 1}` | `by.role("button", name="Save").nth(1)` |
| first of several matches picked silently | strict: pick with `.first`, `.last`, `.nth(i)`, `.within(...)`, `.near(...)` |
| `"12.5s"`, `0.25`, `"downbeat:4"`, `"cue:drop+2f"` as strings or floats | `t("12.5s")`, `t("250ms")`, `t("downbeat:4")`, `t("cue:drop") + t("2f")`; bare numbers are errors |

## Media

Video projects, scores, Blender scenes and web scenes written for the earlier engine keep working with it
until the new scene and score executors land (skills `video-editing`, `scores`, `blender-scenes`,
`web-scenes`). New edits, gfx and mixes go to `@eks.edit` and `@eks.scene(kind="gfx")` (skills
`eks-harness:eks-edit`, `eks-harness:eks-gfx-audio`); examples in `examples/cookbook/.harness/edits/` are
the earlier recipes ported.

## Order of work for a repository

1. App profiles: keep `app.toml`, add `[defaults]` for timeouts, viewports, noise and checks.
2. Runtime: move `app.py` start-up and login code into an `@eks.app` class in a repo plugin; trust it.
3. Seed: express the scenario as `@eks.resource` kinds and an `@eks.seed`; personas come from it.
4. Macros: move shared screen helpers (menus, tenant switching) to `@eks.macro`.
5. Flows: port one at a time; `eks-harness plan` until clean, then `eks-harness run` and compare the
   screenshots with the old ones.
6. Agents, skills and CLAUDE.md of the repository: point them at `eks-harness run` and the eks_* tools.
