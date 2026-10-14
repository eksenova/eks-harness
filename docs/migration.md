# Migrating to the eks API

eks-harness replaced its per-domain APIs (flow apps, driver tools, the score DSL, annotation specs, seeders and
credential providers) with one declarative API, `eks`. Code builds a typed plan, the hub runs it. The old APIs were
removed without compatibility shims, so existing flows, app helpers and plugins need to be ported. This page maps
each old piece to its replacement. The score, studio and legacy video APIs stay until the scene and score
milestone replaces them; they are listed at the end.

Read [overview.md](overview.md) first for the model, and [reference/ops.md](reference/ops.md) for every op.

## Flows

| Old | New |
| --- | --- |
| a file with `def flow(app)` in `<app>/.harness/flows/` | a function decorated with `@eks.flow(app="web-app")` taking `s: eks.Session`, in the same folder or `.harness/flows/` |
| `eks-harness flow run <file> [--platform] [--set] [--backend]` | `eks-harness run <file>[::name] [--platform] [--set] [--backend]` |
| `eks-harness flow list` | `eks-harness plan <file>` per file, or the MCP tool `eks_flows` |
| `eks-harness flow run -e "<script>"` | `eks-harness do <op> key=value ...` for one op, or an MCP session (`eks_session_open`, `eks_session_do`) |
| MCP `flow_run` | MCP `eks_run` (Python source, a file or plan JSON) |
| the flow report | the run report (`eks-harness runs show <run>`, `eks_run_status`) |
| `run_flow`, `FlowRequest` | `POST /api/runs` with a plan or an entry file |

### The app object

| Old (`WebApp`, `MobileApp`) | New |
| --- | --- |
| `app.goto(path)`, `app.nav(path)` | `s.ui.nav(route, params=)` |
| `app.navigate(name, params)` (mobile) | `s.ui.nav(name, params=)` |
| `app.back()`, `app.go_back()`, `app.reload()` | `s.ui.back()`, `s.ui.reload()` |
| `app.click(t)`, `app.press(t)` (mobile) | `s.ui.tap(t)` |
| `app.press(t, long=True)` | `s.ui.long_press(t)` |
| `app.press(key)` (web) | `s.ui.key(key)` |
| `app.fill`, `app.type`, `app.select`, `app.check`, `app.choose`, `app.hover` | `s.ui.fill`, `s.ui.type`, `s.ui.select`, `s.ui.check`, `s.ui.choose`, `s.ui.hover` |
| `app.toggle(t)` (mobile) | `s.ui.check(t)` or `s.mobile.toggle(t)` |
| `app.submit(t)` (mobile) | `s.mobile.submit(t)` |
| `app.wheel(...)`, `app.scroll(...)` | `s.web.wheel(...)`, `s.ui.scroll(target, to=, by=)` |
| `app.wait_for`, `app.wait_gone`, `app.wait_url`, `app.settle`, `app.idle()` | `s.ui.wait_for`, `s.ui.wait_gone`, `s.ui.wait_url`, `s.ui.settle`, `s.mobile.idle()` |
| `app.text`, `app.value`, `app.count`, `app.exists`, `app.visible`, `app.tree`, `app.url`, `app.route` | the same names on `s.ui`; they return Refs |
| `app.state(path)`, `app.dispatch(action)` (mobile) | `s.ui.state(path)` or `s.mobile.state(path)`, `s.mobile.dispatch(action)` |
| `app.js(source, arg)` | `s.eval(source, arg)` |
| `app.api(method, path, body)` | `s.api.get/post/put/patch/delete(path, json=, query=, as_=)` |
| `app.login(...)` | `as_=` on the flow or `s.as_(persona)`, which runs the app runtime's `login` hook |
| `app.arm(slot, ...)`, `app.clear_fakes()` | `s.fake.arm(kind, media=, mode=, fixture=)`, `s.fake.clear()` |
| `app.patch`, `app.unpatch`, `app.inject` (mobile) | `s.mobile.patch`, `s.mobile.unpatch`, `s.mobile.mount` |
| `app.shot(name, ...)` | `s.shot(name, element=, focus=, full=, viewport=)` |
| `app.checkpoint(label)` | `s.checkpoint(label, viewport=, checks=)`; it fails the run by default when a check fails |
| `app.checks()` | the checks of `s.checkpoint`; pick them with `checks=[...]` and `eks.checks.use(kind, **params)` |
| `app.start_recording`, `app.stop_recording`, `app.recording_of(...)` | `with s.record(name, pace=, viewport=, pointer=):` |
| `app.title`, `app.caption`, `app.callout`, `app.highlight`, `app.spotlight`, `app.unannotate` | `s.overlay.title`, `caption`, `callout`, `highlight`, `spotlight`, then `s.overlay.remove(key)` or `s.overlay.clear()` |
| `app.sleep(seconds)` | a condition (`s.ui.wait_for`, `expect.eventually`); inside a recording `s.hold(t("1s"))` |
| `app.note(text)`, `app.emit(name, data)` | node labels and the run journal; notes on artifacts stay in the store tools |
| `assert` on values read from the page | `expect(target).visible()/hidden()/text()/count()/value()` and `expect(ref).eq/contains/matches/...` |
| Python `if` or `for` on page values | `eks.when`/`eks.otherwise`, `eks.each`, or `@eks.dynamic` |
| durations in seconds (`timeout=15`) | `Time` values (`timeout=t("15s")`) |

Targets keep their shorthand strings (`"#test-id"`, `"role=button:Kaydet"`, plain text) and gain `by.*`
builders with `.within`, `.near`, `.nth`, `.first` and `.last`. Matching is strict now: when more than one element
matches, the op fails and lists them, where the old helpers took the first one.

### App helpers and boot

| Old | New |
| --- | --- |
| `<app>/.harness/app.py` with `class App(WebApp)` or `class App(MobileApp)` | an `@eks.app("<profile name>")` class in a repo plugin module or project code |
| `App.prepare()` (backend, ports, dev server) | `prepare(self, ctx)` returning `AppRuntime(url=, api_url=, options=, env=)`; start dev servers with `ctx.spawn(argv, key=, cwd=, env=, ready_url=)` |
| `App.boot()` | `boot(self, ctx, worker)` |
| `App.worker_options()` | `AppRuntime.options` |
| `App.link_backend(...)`, `self.backend` | `[backend]` in `app.toml` or `--backend` on `run`; `ctx.backend_url`, `ctx.backend_info`, `ctx.persona(key)` |
| app helper methods (login screens, menus) and `flow_helpers` plugin classes | `@eks.macro(platforms=[...])` functions, called as `s.<namespace>.<name>(...)` |
| login driver extensions (`web/auth.mjs` and similar) | the app runtime's `login(self, ctx, worker, persona)` hook, or a typed worker op registered with `defineOp` |

`app.toml` keeps its role. Keys the engine reads include `[app] name` and `platforms`, `[web] url`, `dev_port`,
`browser`, `timezone`, `locale`, `[mobile] bridge`, `metro_port`, `ios_bundle`, `android_package` and
`[backend]`.

## Driver and session tools

| Old | New |
| --- | --- |
| MCP `driver_act`, `driver_observe`, `driver_capture` | MCP `eks_do` for one op (it acquires a lease and backend itself), or `eks_session_open`, `eks_session_do`, `eks_session_close` for a sequence of ops on one lease |
| exploring by hand, then writing a flow | `eks_session_export` turns the ops of a session into a `.py` flow |
| `WorkerSession` and `/api/drivers/<sid>/act` and similar routes | the engine's typed op protocol; `/api/drivers/workers` lists running workers |
| worker routes `/goto`, `/click`, `/run`, `/exec`, ... | `POST /op` with a typed op ([drivers.md](drivers.md)) |
| ad-hoc scripts sent to `/exec` | `s.eval(js)` in a flow, or a `defineOp` op in a driver extension |

## Seeds, personas and credentials

| Old | New |
| --- | --- |
| a `seeder` contribution that fills a backend with a scenario | an `@eks.seed(name, backend=, now=, seed=)` function declaring resources, plus `@eks.resource` kinds with `create(self, ctx)` |
| MCP `seed_backend`, `eks-harness backend seed` | `eks-harness run <file>::<seed>` builds and snapshots a seed; a flow with `seed=` restores it |
| a `credentials` contribution with personas | persona kinds (`@eks.resource(..., persona=True)`) whose output carries the login; they become the seed's personas |
| MCP `list_personas` | MCP `eks_seed_show` and `eks-harness seed show <key>` |
| data created by hand for a feature | `d.extend("<base seed>")` plus the new resources, in the branch's seed file |

`seeder` and `credentials` contributions still load, so plugin backends that have not moved yet keep their
scenarios and logins; new code uses seeds and persona kinds, which the engine snapshots, restores and checks for
determinism.

## Backends

| Old | New |
| --- | --- |
| a `backend` contribution entry in `harness-plugin.toml` | an `@eks.backend(name, description=, requires=, identity=, idle_timeout=)` class in a module the manifest lists under `modules` (legacy plugin backends keep working) |
| infrastructure containers started in `prepare()` | a separate backend definition with `identity="global"` or `"host"`, named in the API backend's `requires=` |
| a `backend_policy` contribution | `@eks.backend_policy(name)` |
| MCP `ensure_backend` | MCP `backend_acquire` |
| seed hooks `seed`, `snapshot`, `restore` | `reset`, `seed_phase`, `snapshot`, `restore`, `migrations`, `drop_snapshot` ([reference/resources.md](reference/resources.md)) |

Backend leases, `eks-harness backend acquire|renew|release`, idle timeouts and the `[backends.<definition>]` table
of `.harness/project.toml` work as before; `requires` can also be set there.

## Overlays and annotation

| Old | New |
| --- | --- |
| live annotations through `app.title/caption/callout/...` | `s.overlay.<kind>(...)`, eleven kinds with one model |
| annotation specs (`annotate.Spec`) applied to a screenshot | `eks.annotate(shot, [eks.overlay.callout(...), ...], style=)` in a flow (op `overlay.annotate`) |
| `eks-harness annotate`, the `annotate*` MCP tools | kept; they work on stored artifacts and their versions |
| overlays drawn into a video afterwards | overlays on an edit track: `e.track("notes").overlay(eks.overlay.caption(...))` |

## Video, scores and studio

| Old | New |
| --- | --- |
| a legacy video project (`eks_harness.video` IR, `eks-harness video render`) | `@eks.edit(fps=, size=, seed=)` with tracks, clips, `eks.fx`, `eks.tx` and overlays; `eks-harness run <file>` renders it |
| effect and transition classes of the legacy IR | the same catalog entries as `eks.fx.<kind>`, `eks.tx.<kind>`, `eks.curves.<kind>` ([reference/catalog.md](reference/catalog.md)) |
| hand-written review expectation JSON | `e.expect.cut/beats/text/loudness/...` and `e.expect.derive(...)` |
| Skia closures for motion graphics | `@eks.scene(kind="gfx")` layers with keyframes and beat triggers |
| an audio mix outside the engine | `e.audio.track(...)`, `.duck`, `.compress`, `.eq`, `.fade`, `e.audio.master(loudness=, true_peak=)` |
| `beatsheet.json` and score cue models | `t("beat:N")`, `t("bar:N")`, `t.beats("downbeat")[...]`, `e.cue(name, at)` on the plan clock |
| `score.dsl` scores with device takes | `@eks.score` with `sc.take(...)` and `at=` pins; traced today, executed with the scene and score milestone |

Until that milestone, the legacy video API, `eks-harness video`, `studio`, `score` and `queue`, the studio and
score MCP tools and the device-take cue model stay and keep working.

## Plugin manifests

`harness-plugin.toml` keeps `[plugin]` (id, name, version, api), `[requires]`, `[config]` and gains
`modules = [...]`: the Python modules to import, where the decorators register ops, macros, resources, checks,
catalog entries, renderers, backends, app runtimes and presets. Contribution types without a consumer were
removed. The remaining ones are `driver_extension` (a JS module that registers worker ops with `defineOp`),
`backend`, `backend_policy`, `seeder`, `credentials`, `api`, `cli`, `mcp_tools`, `ui`, `viewer`, `node_job`,
`node_capability` and the media types (`effect`, `transition`, `easing`, `marker_source`, `captioner`, `encoder`,
`media_provider`, `media_renderer`, `scene_engine`, `score_track`, `video_check`).

## A ported flow

Before:

```python
def flow(app):
    app.goto("/tenant/settings/branches")
    app.wait_for("text=Kadıköy")
    app.checkpoint("liste")
    branches = app.api("GET", "/api/tenant-branches")
    kadikoy = next(b for b in branches["items"] if b["name"] == "Kadıköy")
    app.goto(f"/tenant/invoice/list?branchId={kadikoy['id']}")
    assert app.count("#invoice-row") >= 1
```

After:

```python
import eks
from eks import by, expect


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def subeler(s: eks.Session):
    s.ui.nav("/tenant/settings/branches")
    expect(by.text("Kadıköy")).visible()
    s.checkpoint("liste")
    branches = s.api.get("/api/tenant-branches")
    kadikoy = branches.items.find(name="Kadıköy").id
    s.ui.nav(f"/tenant/invoice/list?branchId={kadikoy}")
    expect(by.test_id("invoice-row")).count(at_least=1)
```

The ported flow names its seed and persona, so the run restores the seed into the backend and logs in before the
first node; the API read is a Ref resolved at run time, and the checks fail the run instead of returning a report
to read.
