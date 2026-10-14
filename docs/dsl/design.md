# eks: one declarative API for eks-harness

Status: design, agreed interactively on 2026-10-11. Branch `feature/unified-dsl` from `211ff45`.

## 1. Goal

One Python API controls everything eks-harness does: seeding data, driving web and
mobile apps, tests and checks, screenshots and recordings, overlays and annotation,
video editing, 2D motion graphics, audio mixing, Blender and web scenes, scores that
sync all of it to music, breakpoints, network control and script injection. The API
is declarative (code builds a typed plan, an engine runs it), deterministic,
extensible with custom ops, macros, resources and definitions, and it replaces the
current APIs outright.

## 2. Decisions

| Topic | Decision |
| --- | --- |
| Scope | Phase 1: eks-harness. Phase 2: migrate the eksenova repo (flows, app.py, plugin, seed, eksenads). |
| Compatibility | Clean break. Old flow `App`, `WorkerSession`, `score.dsl`, `annotate.Spec`, studio tools, video IR authoring, legacy worker routes and `/exec` are removed, not shimmed. |
| Paradigm | Build a plan, then run it. Python builds a typed, serializable plan graph; the engine validates, caches, schedules and executes it. |
| Runtime values | Ops return lazy `Ref[T]`. Control flow through `eks.when` / `eks.each`; `@eks.dynamic` is the escape hatch that runs at execution time and returns a sub-plan. |
| Time | One `Time` type, integer ticks (1/705 600 000 s), one grammar everywhere. Bare numbers are rejected. |
| Assertions | Fail by default. `expect` and automatic checkpoint checks fail the run unless noise/allow lists or `expect.soft` say otherwise. |
| Targets | One `by.` model plus string shorthand for web, iOS, Android, overlays and scene anchors; platform predicates checked at plan time; strict matching. |
| Worker protocol | Typed op RPC with schemas; TS types generated from the pydantic models; JS extensions register ops with `defineOp`. `/exec` and legacy routes go. |
| Blender | Declarative scene graph compiled to bpy, plus a hashed `@scene.bpy` escape hatch. |
| Executor | The hub executes plans. Clients build and validate; `POST /api/plans`. |
| Seed | Declarative typed resources, frozen `now`, root seed, path-derived ids, snapshot keyed by migrations + plan hash. Personas come out of the seed graph. |
| Overlays | One overlay model, three renderers: live in-app, post on screenshots, post on video. |
| MCP | Plan-centric tools, flow management tools, a stateless one-shot op tool, store tools; built-in skills. |
| Ads | `eks.gfx` (declarative 2D layers) and `eks.audio` (mix graph) inside the engine; eksenads becomes templates and macros; `beatsheet.json` is replaced by the score clock. |
| Naming | `import eks`; files in `.harness/{flows,seeds,edits,scenes,scores}/`; `eks-harness run file.py[::name]`. |
| Network | Route mocking, deterministic shaping, record/replay cassettes. |
| Tooling | Recorder to DSL codegen, plan debugger in the web UI, plan diff on PRs, type stubs and per-op JSON Schema. |
| Check families | Visual regression, performance budgets, exploration/fuzz. |
| Extensions | Python registry with typed decorators; `harness-plugin.toml` keeps identity, trust, requirements and settings only. |
| Sleeps | No sleep op. Waits are conditions. Pacing is a recording-only profile plus `hold(t)`. |
| Device takes | A take is a flow whose nodes can be pinned to Time; the footage is warped so pinned nodes land exactly. |
| Python | 3.13 minimum. |
| Docs | Comprehensive skills and documentation in the Claude Code plugin (section 15). |

## 3. Architecture

```text
 authoring (client, no IO)          hub (daemon)                              executors
 ------------------------          ----------------------------------         -------------------------
 import eks                        /api/plans  validate, plan, diff           web worker   (typed RPC)
 @eks.flow/seed/edit/scene/score   /api/runs   schedule, journal, events      mobile worker + RN bridge
 builders -> Ref, Time, Target ->  engine      resources: leases, backends,   api / http
 PlanIR (JSON, Merkle hashed)                  seeds, cassettes, nodes        seed resources
                                               cache (content addressed)      render: edit, gfx, audio,
                                               debugger (break, resume)         blender, web scene
                                               dynamic sandbox                review checks, store
```

Packages:

| Package | Role |
| --- | --- |
| `eks` | Public authoring SDK. Builders, decorators, `Ref`, `Time`, `Target`, registry. Imports nothing heavy; safe to import anywhere. |
| `eks_harness.ir` | Pydantic models of the plan: `Plan`, `Node`, `Ref` encodings, control nodes, resource nodes, op argument models. Single JSON Schema. |
| `eks_harness.engine` | Validator, resource planner, scheduler, executor registry, journal, cache, debugger, dynamic sandbox. |
| `eks_harness.ops.*` | Built-in op families: `ui`, `web`, `mobile`, `api`, `net`, `inject`, `capture`, `overlay`, `check`, `data`, `edit`, `gfx`, `audio`, `blender`, `scene`, `score`, `store`, `debug`. Each family = arg models + executor. |
| `workers/kit` | Shared TS op loop: schema validation, step events, breakpoints, journaling, errors. |
| existing internals | Pools, leases, backends, store, nodes, farm, ffmpeg graph and frame pipeline, review checks: kept as engine internals and refactored where the plan needs it. |

## 4. Authoring model

### 4.1 Entry points

```python
import eks
from eks import by, t, expect

@eks.flow(app="web-app", seed="standard", as_="harman-admin", tags=["subeler"])
def subeler(s: eks.Session):
    branches = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
    kadikoy = branches.items.find(name="Kadıköy").id
    s.ui.nav("/tenant/settings/branches")
    expect(by.text("Kadıköy")).visible()
    s.checkpoint("liste")
    s.checkpoint("liste-telefon", viewport="phone")
    s.ui.nav(f"/tenant/invoice/list?branchId={kadikoy}")
    expect(by.test_id("invoice-row")).count(at_least=1)
    return {"branches": branches}
```

| Decorator | Produces | Folder |
| --- | --- | --- |
| `@eks.flow` | a driving plan on one platform (or `platforms=[...]` matrix) | `.harness/flows/` and `<app>/.harness/flows/` |
| `@eks.seed` | a resource graph for a backend | `.harness/seeds/` |
| `@eks.edit` | a video timeline | `.harness/edits/` |
| `@eks.scene` | a Blender, web or gfx scene | `.harness/scenes/` |
| `@eks.score` | a clock that binds takes, scenes, edit and audio | `.harness/scores/` |
| `@eks.macro` | a reusable composite (sub-plan) | anywhere, usually plugins |
| `@eks.op` / `@eks.resource` / `@eks.check` / ... | extensions (section 11) | plugins or project code |

Decorators register metadata; calling `eks.plan(fn, **params)` traces the function with
builder objects and returns a `Plan`. Tracing runs no IO. Parameters are typed by the
function signature and validated (`--set` values are parsed into those types).

### 4.2 Refs

* Every op returns a typed `Ref[T]` (`Ref[Json]`, `Ref[str]`, `Ref[int]`, `Ref[bool]`, `Ref[Artifact]`, ...).
* Attribute, index and a small set of methods (`find`, `filter`, `map` with lambdas over
  Refs is not allowed; `find(**eq)`, `first`, `len`) build path expressions, encoded as
  `{"$ref": "n12", "path": [...]}`.
* String interpolation: `Ref.__format__` emits a sentinel token; builders that accept
  strings parse sentinels into `{"$fmt": ["...", {"$ref": ...}, "..."]}`. Python 3.13
  has no t-strings, so this is the mechanism; it is exact because only builders consume
  the strings.
* `bool(ref)` raises: conditions must go through `eks.when`.

### 4.3 Control flow

```python
with eks.when(s.ui.exists("#empty-state")):
    s.ui.tap("#create")
with eks.otherwise():
    s.ui.tap(by.test_id("row").first)

with eks.each(s.api.get("/api/invoices").items[:3]) as inv:
    s.ui.nav(f"/tenant/invoice/{inv.id}")
    s.checkpoint(f"fatura-{inv.number}")

with eks.parallel():                     # independent lanes, e.g. web and api
    ...

@eks.dynamic                              # escape hatch: real values, returns a sub-plan
def per_row(n=s.ui.count("#row")):
    return [s.ui.tap(by.test_id("row").nth(i)) for i in range(n)]
```

`eks.each` over a Ref expands at run time into a journaled sub-plan; over a literal it
expands at plan time. Dynamic functions run in a sandboxed subprocess on the hub; their
source hash is part of the node hash.

### 4.4 Time

```python
t("1.5s"); t("300ms"); t("f:120"); t("beat:8") + t("1b"); t("bar:4")
t("cue:logo") - t("2f"); t("word:'EksenGOLD'"); t("marker:intro#2"); t.start; t.end
t.span(t("0s"), t("15s"))
t.beats("downbeat")[0:8]                  # a TimeSet, usable wherever a trigger is accepted
```

* Internal unit: integer ticks, 705 600 000 per second (divisible by 24, 25, 30, 48, 50,
  60, 120 fps and 44.1/48/96 kHz). Frames are exact.
* Symbolic times (`beat`, `bar`, `cue`, `marker`, `word`) resolve against the active
  clock; `bar:N` is a real bar (time signature aware), not an alias for downbeat.
* Every duration-like parameter (`hold`, `timeout`, `at`, `start`, `fade`, `delay`) is
  `Time`. Plain numbers are a type error.

### 4.5 Targets

```python
by.test_id("credit-add-submit")        # "#credit-add-submit"
by.role("button", name="Sunuyu iptal et")   # "role=button:Sunuyu iptal et"
by.text("Kadıköy"); by.text("Kadı", exact=False)   # bare string = text
by.label("E-posta"); by.placeholder("Ara")
by.css("li[data-package-id='3']")      # web only
by.kind("button")                      # react native only
by.test_id("list").within(by.role("dialog")).nth(2)
by.text("Manuel").near(by.text("Şablon ile"))
by.point(120, 480)                     # coordinates, for overlays and gestures
```

* One `Target` model serves drivers, overlays (anchors), Blender and web scene anchors
  and visual-regression masks.
* Platform predicates are checked at plan time against the target platform's capability
  set.
* Strict: zero matches waits until timeout; more than one match fails with the
  candidates listed unless `.nth()`, `.first` or `.last` is given.

### 4.6 Session surface (flows)

`s.ui` is platform neutral, `s.web` and `s.mobile` add platform specifics.

| Group | Ops |
| --- | --- |
| navigation | `nav(route, params=)`, `back()`, `reload()`, `open_url(url)`, `deeplink(url)` |
| input | `tap`, `double_tap`, `long_press`, `hover`, `fill`, `type(pace=)`, `clear`, `key`, `select`, `check`, `choose(trigger, option)`, `scroll(target, to=/by=)`, `swipe`, `drag` |
| reads (Refs) | `text`, `value`, `attr`, `count`, `exists`, `visible`, `tree`, `url`, `route`, `state(path)`, `layout` |
| waits | `wait_for(target, state=, timeout=)`, `wait_gone`, `wait_url`, `settle(strict=True)` |
| assertions | `expect(target).visible()/hidden()/text(...)/count(...)/value(...)`, `expect(ref).eq/contains/matches`, `expect.soft(...)`, `expect.eventually(...)` |
| evidence | `checkpoint(label, viewport=, checks=)`, `shot(name, element=, full=)`, `dump("dom"|"a11y"|"mhtml")`, `har()` |
| recording | `with s.record(name, pace=, viewport=, pointer=):`, `hold(t)` (recording only) |
| overlays | `s.overlay.title/caption/callout/spotlight/highlight/arrow/blur/redact/icon/image/step` |
| backend | `s.api.get/post/put/delete`, `s.as_(persona)`, `s.backend.info` |
| fakes | `s.fake.arm("camera", media=..., mode=)`, `s.fake.clear()` |
| network | `s.net.route(...)`, `s.net.shape(profile)`, `s.net.cassette(name, mode=)` (section 8) |
| injection | `s.inject.script(js, when="init"|"now")`, `s.inject.style(css)`, `s.mobile.patch/unpatch/mount`, `s.eval(js)` (returns Ref) |
| store | `s.mobile.dispatch(action)`, `s.mobile.state(path)` |
| debug | `s.debug.break_(label)`, `s.debug.pause_on_error()` (section 9) |

Session-level defaults (timeouts, pace, viewport, noise lists) come from the app profile
and can be overridden per flow and per op.

Resources of a session: a flow always holds its browser or device lease, and holds a backend
only when it needs one, that is when it names `seed=`, `backend=` or `as_`, or calls an op that
uses the backend (`api.*`, `session.as`, `backend.info`, `data.*`). A UI-only flow runs with
no backend at all. A take holds a backend only when its session does. Code inside
`@eks.dynamic` cannot add a backend at run time; such a flow names its backend explicitly.

Native UI outside the app (BL-011, M2): system permission prompts, OS sheets and other dialogs
outside the React Native tree are reachable through a `native` target surface on iOS and
Android (Android uiautomator plus input events; iOS XCUITest, WebDriverAgent or idb), so
`s.ui.tap(by.text("İzin ver").native)` and `expect(by.text("İzin ver").native)` work on them, plus a plan-level
policy that auto-accepts or denies permission prompts.

## 5. Plan IR and engine

### 5.1 IR

```json
{
  "schema": "eks/plan@1",
  "kind": "flow",
  "name": "subeler",
  "params": {"tenant": "harman"},
  "clock": {"now": "2026-01-15T09:00:00+03:00", "seed": 2026, "fps": null},
  "resources": [{"id": "r1", "kind": "lease.web", "app": "web-app"},
                {"id": "r2", "kind": "backend", "definition": "eksenapi", "seed": "standard"}],
  "nodes": [
    {"id": "n1", "op": "api.get", "args": {"path": "/api/tenant-branches"}, "uses": ["r2"]},
    {"id": "n2", "op": "ui.nav", "args": {"route": {"$fmt": ["/x?id=", {"$ref": "n1", "path": ["items", {"find": {"name": "Kadıköy"}}, "id"]}]}},
     "uses": ["r1"], "after": ["n1"], "src": "flows/subeler.py:9"}
  ],
  "hash": "sha256:..."
}
```

* Node ids are stable: derived from the source position and call path, so a plan diff
  matches nodes across edits.
* Each node hash covers op, args (with referenced node hashes), op version and executor
  version: a Merkle DAG.
* Ordering: nodes on the same resource lane run in program order; independent lanes may
  run in parallel (`eks.parallel` or the scheduler for pure nodes such as renders).

### 5.2 Engine

| Part | Does |
| --- | --- |
| validator | arg schemas, Ref types, target capabilities per platform, time resolvability, resource availability, plugin versions |
| resource planner | turns `resources` into lease acquisitions, backend leases, seed snapshot restores, cassette loads, render node slots |
| scheduler | runs the DAG per lane; retries only where an op declares itself idempotent |
| executors | registry `op -> executor` with location (hub, worker, node, blender, browser scene) |
| journal | SQLite: `runs`, `run_nodes`, `node_events` with inputs, outputs, timings, artifacts, errors |
| cache | content-addressed results for pure nodes (renders, seed snapshots, analysis, gfx, audio) |
| debugger | breakpoints by node id, op, label or condition; pause on error; resume, retry, skip, edit-and-resume |
| dynamic sandbox | subprocess with rlimits and no network for `@eks.dynamic` and edit/scene build code |

### 5.3 Run results

A run returns one report: status, failures with node, source line, checks, failure
shots; checkpoints; artifacts with links; values; timings; the journal id. The CLI, MCP
and UI all render the same model.

### 5.4 Process ownership (BL-017, M2)

Every process the harness spawns has an owner and dies with it; nothing is left running with
PPID 1.

* Driver workers (web and mobile driver servers) and app dev servers (`next dev`,
  `expo start`, Metro, `vite`) start in their own process group. The spawning process kills
  the group on exit, in `atexit` and in its SIGTERM and SIGINT handlers.
* Workers also watch their parent themselves: they exit on stdin EOF or IPC disconnect, or when
  `process.ppid` becomes 1, so even a `kill -9` of the owner does not leave them behind. As a
  backstop, a worker exits after an idle timeout with no client.
* Every spawned process is registered in the procs directory, as `pools.backends` supervision
  already does, with its pid, start time, owner pid and owner start time, kind, tree and
  install path.
* The hub reaps stale entries on startup and on its housekeeping timer: when the owner is dead
  (checked by pid and start time, so a reused pid is not mistaken for it), the process group is
  killed and the entry removed.
* `eks-harness reap` (also `doctor --kill-orphans`) does the same on demand and also finds
  orphans of other installs (uvx archives, uv tools, build environments, in-repo checkouts) by
  matching `eks_harness/_workers/*/src/server.mjs` and harness dev server command lines with
  PPID 1. It lists them first and kills only after confirmation (`--yes` without a terminal).
* A test kills the harness with SIGKILL and asserts that no worker or dev server survives.

## 6. Determinism contract

| Source | Rule |
| --- | --- |
| clock | Plan `now` is frozen. Web: Playwright clock install. React Native: bridge Date shim. Seed: resources use plan `now`. Local backends get plan `now` through an injectable server clock (section 17). |
| randomness | One root seed; every node gets `hash(root, node path)`. Effects, curves, gfx, Blender (`cycles.seed`), web scenes (`Math.random`, `crypto.getRandomValues`) all take derived seeds. |
| ids | Seed resources and generated values (TCKN, VKN, IBAN, ULID) derive from the resource path. |
| waits | Conditions only, with Time timeouts; `settle` throws on timeout in strict mode (default for tests). |
| network | Optional cassette replay for hermetic runs. |
| caches | Keys include everything that changes output: fps, resolution, seeds, resolved markers, plugin and executor versions, chosen encoder, backend versions. |
| Blender | Per-clip platform pinning; device and samples explicit; engine aliases resolved at plan time. |
| gfx | GPU render keyed by adapter and driver; one machine per clip; CPU reference path for goldens. |
| flakiness | `eks-harness run --repeat N` compares journals and reports diverging nodes. |

## 7. Data and seeds

```python
@eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
def standard(d: eks.Data):
    host = d.host_persona("host")
    bayi = d.federation("anadolu-bayi", type="Reseller", permissions=["*"],
                        exclude=["Kts.Federations.*"])
    harman = d.tenant("harman", name="Harman Kuyumculuk", reseller=bayi)
    harman.persona("harman-admin", role="admin")
    d.accountant("yildiz", name="Yıldız Mali Müşavirlik").serves(harman)
    kt = harman.bank_account("kuveyt", currency="TRY", opening=250_000)
    kt.transactions(n=120, profile="kuyumcu")
    harman.invoices(states="*", per_state=3)
```

* Resource kinds are plugin classes (`@eks.resource`) with typed fields, a parent, and
  `create(ctx)` (API or SQL), optional `phase` (schema, api, finalize, mongo).
* The engine orders creation by the resource graph, records outputs (ids) per resource
  and builds a snapshot keyed by `(backend migrations hash, seed plan hash)`.
* Personas are seed outputs; `eks.Persona` refs in flows are checked at plan time against
  the seed.
* Flows name a seed; the resource planner restores the snapshot into the backend lease.
* `d.extend(base="standard")` composes scenarios; a feature branch adds resources instead
  of editing data by hand.

### 7.1 Backend dependencies and shared infrastructure (BL-016, M3)

Shared infrastructure (database servers, object stores, document renderers) is an ordinary
backend definition that other backends depend on, so the hub owns its lifetime instead of a
plugin's `prepare()` starting containers that never stop.

```python
@eks.backend("sellersgo-infra", identity="global", idle_timeout=t("600s"))
class Infra: ...            # start (docker start/run), health, info, stop, cleanup

@eks.backend("sellersgo-api", requires=["sellersgo-infra"])
class Api:
    def prepare(self, ctx):
        pg = ctx.dependencies["sellersgo-infra"]     # BackendInfo: url, ports, credentials, env, extra
        ...
```

* `requires` names other backend definitions, optionally with params or a key
  (`requires=[eks.requires("x", key="pg16", params={...})]`); `.harness/project.toml`
  `[backends.<def>] requires = [...]` can add to it.
* Before a dependent instance starts (before `prepare`), the hub acquires a lease of kind
  `dependency` on each dependency instance, held by the dependent instance, waits until it is
  ready and passes its `BackendInfo` to every hook of the dependent as
  `ctx.dependencies[name]`.
* A dependency lease lives exactly as long as the dependent instance. Stopping, cleaning up or
  replacing the dependent releases it; a replacement hands the lease to the new generation
  without dropping it in between.
* With no dependent left, the dependency follows the normal lease rules: its own idle timeout
  runs, then the hub stops it and runs its `cleanup` (for example `docker stop` and pruning all
  but the newest template database). Volumes are kept, so the next start takes seconds.
* Infrastructure identity is usually `global` or per host, not per tree, so every tree and
  every parameter variant (for example `paddle=sandbox`) shares one instance.
* Only the hub decides when infrastructure stops, which avoids the race between a plugin's
  "is anyone else alive" check and an instance the hub is starting.
* Hub restart reconciliation recreates missing dependency leases for surviving dependents and
  releases orphaned ones.
* Visibility: backend list and status, MCP tools and the web UI show dependency edges (which
  instances hold an infrastructure instance) and its idle countdown.
  `eks-harness backend stop <infra>` refuses while dependents run, unless `--force`, which
  stops the dependents first.
* Dependency cycles are rejected when definitions load; the depth is limited.
* Plugins never start harness-owned containers with `--restart unless-stopped`; the
  infrastructure backend owns their lifetime. Consumers: the SellersGo plugin
  (`sellersgo-infra`: Postgres and S3, template pruning) and the eksenova plugin
  (`eksengold-infra`: MSSQL, Mongo, Gotenberg) drop their container start code from the API
  backend's `prepare`.

## 8. Network and injection

```python
s.net.route("GET", "/api/bank-transactions*").delay(t("800ms"))
s.net.route("POST", "/api/invoices").respond(status=500, json={"error": "x"})
s.net.route("GET", "/api/rates").patch(lambda_path="items[0].rate", value="2450.10")
s.net.shape("3g")                                  # deterministic profile
with s.net.cassette("banka-arama", mode="replay"): # record | replay | auto
    ...
s.inject.script("window.__flags = {beta: true}", when="init")
```

Web uses Playwright routing; React Native uses a bridge fetch/XHR shim with the same
matcher model. Cassettes are artifacts keyed by plan and backend fingerprint.

## 9. Debugging

* `s.debug.break_("before-submit")` in source, or `eks-harness run --break n12 --break op:ui.tap --break-on-error`.
* A paused run exposes the node, its resolved args, the screen or DOM snapshot, logs and
  the Refs computed so far. Actions: continue, step, retry, skip, abort, edit args and
  resume, run an ad-hoc op in the paused session.
* Same controls in the CLI (interactive prompt), MCP (`run_control`) and the web UI plan
  debugger.

## 10. Media: overlays, edits, gfx, audio, scenes, scores

### 10.1 Overlays

One model: `title, caption, callout, spotlight, highlight, arrow, blur, redact, icon,
image, step`, each with a Target anchor, Time window, placement and style. Renderers:

| Renderer | When |
| --- | --- |
| live | inside the app while recording (`s.overlay.*` in a flow) |
| screenshot | `eks.annotate(artifact, [overlays], style=)`; versioned, re-renderable, re-capturable |
| video | overlays in an edit track, rendered by the edit engine (all kinds, including callout and spotlight) |

### 10.2 Edits

```python
@eks.edit(fps=30, size=(1080, 1920), seed=7)
def demo(e: eks.Edit):
    clip = e.media("artifact:VID123")
    music = e.audio.track("music/song.ogg", gain=-6).beats("beat", "downbeat")
    v = e.track("main")
    v.clip(clip, start=t("0s"), src=t.span(t("2s"), t("14s")))
     .effect(eks.fx.zoom(1.0, 1.08, ease="out_cubic"))
     .transition_out(eks.tx.crossfade(t("8f")))
    e.track("overlays").overlay(eks.overlay.caption("Kontör işlemleri", at=t.span(t("1s"), t("4s"))))
    e.expect.cut(at=t("beat:8"))                 # review checks generated from the edit
```

* The video IR is rebuilt on `Time` and `Target`; effect, transition, easing and curve
  catalogs are kept and moved to the registry.
* Known engine bugs are fixed on the way: segment-local versus absolute time, primary
  track ignoring `start`, `BeatRef.every`, graph path dropping easing, unknown curve
  kinds in the ffmpeg lowering, cache keys, effect seeds, unread `RenderSettings`.
* Review expectations are derived from the edit (cuts, beats, captions) instead of
  hand-written JSON.

### 10.3 gfx and audio

* `eks.gfx`: declarative 2D layers (text, shapes, paths, images, masks, groups, video
  textures) with properties animated by keyframes, curves, beats and easings. Replaces
  the eksenads Skia closures and duplicate easing helpers.
* `eks.audio`: a mix graph (tracks, stems, gain, EQ, compression, ducking and sidechain,
  loudness target, true peak limit) rendered inside the engine. Replaces the eksenads
  out-of-engine mix.

### 10.4 Scenes

```python
@eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
def phone(sc: eks.BlenderScene, screen: eks.Media):
    p = sc.load("assets/phone.blend", obj="Phone")
    p.material("Screen").texture("screen", screen)
    cam = sc.camera(lens=50).look_at(p)
    cam.animate("location", {t("bar:0"): (0, -4, 1), t("bar:4"): (0, -2, 1)}, ease="out_cubic")
    sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

    @sc.bpy
    def rig(bpy, ctx): ...
```

* The Blender scene graph compiles to one bpy program run by the existing bootstrap and
  farm; frame cache and fingerprints are kept.
* Web scenes keep the `@eks-harness/scene` runtime (virtual clock, seeded randomness
  added); their props, inputs and events are declared through the same API.
* Scenes emit events back into the score (fixed point, as today).
* Render farm inputs (BL-012 to BL-015):
  * Sync excludes are unanchored and exclude caches and outputs by default (`.cache`, `cache`,
    `renders`, `__pycache__`, `.git`); a project or scene extends the list, never has to repeat it.
  * Sync bundles are content-addressed per file: a manifest of file hashes plus one blob per file,
    so a changed file uploads only that file and unchanged inputs are never stored twice.
  * The hub prunes blobs on a timer while it runs, not only at startup; sync bundles have a
    shorter retention than render outputs.
  * A render whose target node is this machine runs on the real paths with no pack, upload or
    unpack.

### 10.5 Scores and takes

```python
@eks.score(fps=30, song="music/song.ogg", window=t.span(t("0s"), t("15s")))
def ad(sc: eks.Score):
    take = sc.take(platform="ios", app="mobile-app", seed="standard", as_="harman-admin")
    with take.flow() as s:
        s.ui.tap("#new-invoice", at=t("bar:1"))
        s.ui.fill("#amount", "5", at=t("beat:6"))
        s.overlay.callout("#total", "Anlık kur", at=t("bar:2"))
    phone = sc.scene(phone_scene, screen=take)
    sc.edit.track("scene").clip(phone)
    sc.edit.track("graphics").clip(sc.gfx(titles))
    sc.audio.master(loudness=-14)
```

A take runs its flow as fast as it settles, records it, and warps the footage so every
pinned node lands on its time. A pinned node that fails fails the take. The same rules
work in live preview.

## 11. Extensibility

| Decorator | Contributes |
| --- | --- |
| `@eks.op(family, executor=, location=)` | a new primitive with an arg model and an executor |
| `@eks.macro(platforms=)` | a composite that returns a sub-plan; appears in `s.<namespace>` |
| `@eks.resource(kind, parent=)` | a seed resource kind |
| `@eks.check(kind)` | a checkpoint or media check |
| `@eks.effect` / `@eks.transition` / `@eks.easing` / `@eks.curve` | edit catalog entries |
| `@eks.renderer(media_kind)` | a media renderer |
| `@eks.overlay_kind` | a new overlay type with its three renderers |
| `@eks.target_kind` | a new target predicate with per-platform resolvers |
| `@eks.backend` / `@eks.backend_policy` | backends (kept from today's SDK, typed) |
| `@eks.preset(name)` | named definitions: personas, pace profiles, styles, viewports, network profiles |

JS worker ops: `defineOp({name, schema, capabilities, run})` in a driver extension;
schemas are checked against the Python arg model at load time.

`harness-plugin.toml` keeps `[plugin]`, `[requires]`, `[config]` and a list of Python
modules to import; trust and isolation stay as they are.

## 12. Surfaces

### 12.1 CLI

```text
eks-harness run <file>[::name] [--set k=v] [--platform] [--backend <lease>] [--break ...] [--repeat N]
eks-harness plan <file>[::name] [--json] [--diff <git-ref>]
eks-harness ops [name]                 # catalog with schemas
eks-harness record --app web-app       # recorder -> DSL source
eks-harness debug <run-id>             # attach to a paused run
eks-harness runs list|show|replay
eks-harness do <op> [args]             # stateless one-shot op (acquires what it needs)
```

Exit codes of `run`, `runs` and `debug`: 0 passed, 1 failed, errored or invalid plan, 12 paused
without a terminal (attach with `eks-harness debug <id>`), 13 aborted; the CLI's existing codes
(2 usage, 3 hub unreachable, ... 11 nothing selected) keep their meaning. `--repeat N` links
every run to the first (`repeat_of`), compares them and exits with the worst result.

### 12.2 MCP

| Tool | Purpose |
| --- | --- |
| `eks_run` | run Python source, a file or plan JSON; returns the report |
| `eks_plan` / `eks_validate` / `eks_diff` | build, check and compare plans without running |
| `eks_ops` | op catalog and schemas |
| `eks_do` | stateless one-shot op without script or session (waits for leases and backends) |
| `eks_session_open` / `eks_session_do` / `eks_session_close` | interactive step-by-step session |
| `eks_session_export` | the session journal as a `.py` flow |
| `eks_flows` / `eks_flow_get` / `eks_flow_save` / `eks_flow_delete` | list, read, create and edit flows, seeds, edits, scenes, scores |
| `eks_run_control` / `eks_run_status` | debugger controls and run status |
| store tools | artifacts, sessions, shares, notes, tags, retention (kept) |

### 12.3 HTTP and web UI

`/api/plans` (validate, plan, diff), `/api/runs` (start, status, events stream, control,
journal), `/api/ops`. The web UI gets a run page with the plan graph, the journal
timeline, the debugger, golden approvals and the recorder. Video artifacts autoplay wherever
they open (artifact pages, share links, run and studio views), falling back to muted playback
with an Unmute control when the browser blocks sound, as eks-harness main does since e6c011a.

## 13. Checks

| Family | Detail |
| --- | --- |
| checkpoint | layout spill, horizontal scroll, broken images, console and page errors, failed requests, error toasts, route; fail by default with noise lists |
| visual regression | golden per checkpoint and viewport, perceptual diff, masks as Targets, approval in the UI, goldens stored as artifacts |
| performance budgets | web vitals (LCP, CLS, INP), request count and bytes per step; React Native JS frame drops; budgets in the app profile or per flow |
| exploration / fuzz | model-based exploration of screens; crashes and errors are written as reproducing flows |
| media | existing review kinds (cut, beats, onset, loudness, text OCR, motion, black, frozen, safe area), driven by expectations derived from edits |

## 14. What is removed

`flows/core.py`, `flows/web.py`, `flows/mobile.py`, `drivers/sessions.py`, the worker
legacy routes and `/exec`, `score/dsl.py` and `score/takes.py`, `video_device_take` cue
model, `annotate/spec.py` (replaced by the overlay model), studio tool functions and the
per-domain MCP tools (`driver_*`, `video_*`, `score_*`, `flow_run`), unused contribution
types, the four pace definitions (one pace preset remains).

## 15. Claude Code plugin: skills and documentation

Comprehensive skills and docs ship with the plugin (`claude/`), generated where possible
so they cannot drift:

| Skill | Covers |
| --- | --- |
| `eks-harness` | overview, routing to the other skills, the plan model in one page |
| `eks-dsl` | core concepts: plans, Refs, Time, Targets, control flow, determinism rules |
| `eks-flows` | sessions, ui/web/mobile ops, expect, checkpoints, recordings, overlays |
| `eks-data` | seeds, resources, personas, backends and leases |
| `eks-network` | routes, shaping, cassettes, injection |
| `eks-debugging` | breakpoints, pause, resume, journals, the recorder, the plan debugger |
| `eks-checks` | checkpoint checks, visual regression, performance budgets, exploration |
| `eks-edit` | edits, effects, transitions, overlays on video, review expectations |
| `eks-gfx-audio` | 2D motion graphics and the audio mix |
| `eks-scenes` | Blender scene graph, `bpy` escape hatch, web scenes, render farm |
| `eks-scores` | clock, beats, pinned takes, live preview |
| `eks-extending` | ops, macros, resources, checks, renderers, presets, JS `defineOp`, plugin manifests |
| `eks-mcp` | agent workflow: one-shot ops, sessions, exporting flows |
| `eks-migration` | old API to new API mapping, for phase 2 and external users |
| `troubleshooting` | kept, rewritten |

Docs in `docs/`: a reference generated from the op registry and JSON Schemas (every op,
arg, default, platform, example), a cookbook of complete examples per area, the
determinism contract, the plan IR spec, and the migration guide. Agents (`web-harness`,
`ios-harness`, `android-harness`, `studio`) and commands (`/flow`, `/render`, `/score`,
`/harness`) are rewritten for the new API.

## 16. Milestones

Phase 1, eks-harness:

| # | Milestone | Done when |
| --- | --- | --- |
| M0 | Foundations: `eks` package, IR, Time, Target, Ref, registry, tracing, `plan` CLI, JSON Schema, stubs | plans for every example in this doc build and validate |
| M1 | Engine and hub: `/api/plans`, `/api/runs`, scheduler, journal, cache, resource planner, `run` CLI, core MCP | a plan with api and fake executors runs end to end in tests |
| M2 | Drivers: TS op loop, typed web, mobile and RN bridge ops, expect, checkpoints, recordings, live overlays, network, injection, debugger | the web and mobile reference flows pass on real leases |
| M3 | Data: resources, seed engine, snapshots, personas from seed | a sample seed builds, snapshots and restores deterministically |
| M4 | Media: edit IR on Time, engine fixes, overlay renderers, review from edits, gfx, audio | cookbook edits render byte-identical twice |
| M5 | Scenes and scores: Blender scene graph, web scenes, pinned takes, live preview | a sample score with a take, a Blender scene and gfx renders |
| M6 | Tooling: recorder, plan debugger UI, plan diff, visual regression, perf budgets, exploration | each has a test and a doc page |
| M7 | Surfaces and docs: final MCP, skills, generated reference, cookbook, removal of old code | no references to removed APIs remain |

Phase 2, eksenova: plugin rewritten as macros and resources (login, as, impersonate,
personas), the standard seed as resources, 28 web and 18 mobile flows ported, app
profiles, eksenads as templates on gfx, audio, Blender scenes and scores, the
`eksengold-apps` skill and CLAUDE.md updated.

Phase 3, sellersgo (`~/repos/ayomsoft/sellersgo-be` and `~/repos/ayomsoft/sellersgo-fe`,
after phase 2). Their harness setup lives so far only in the `feature/subscription-billing`
(be) and `feature/subscription-billing-ui` (fe) worktrees, untracked; the wave starts from
wherever it has landed by then.

| Repo | Today | Migrates to |
| --- | --- | --- |
| sellersgo-be | plugin `sellersgo-be`: backend `sellersgo-api` (local build, processes, api proxy), seeder `scenarios` (Node seed scripts, fake ShipStation server), credentials `local-personas`, Paddle helpers | `@eks.backend` with typed info, the seed as `@eks.resource` kinds and a `@eks.seed` graph with frozen `now`, personas from the seed graph, Paddle and ShipStation fakes as `s.net` routes or resources |
| sellersgo-fe | plugin `sellersgo-fe`: backend policy `sellersgo-branch`, credentials `personas`, driver extension `web/auth.mjs`, flow helpers `WebHelpers`; `app.py`/`app.toml`; 174 flows (4 checks plus QA suites A, A2, B, C, D, E1, E2, F1, G1, H1) | login and session helpers as `@eks.macro` plus a typed JS `defineOp` where needed, `@eks.backend_policy`, every flow ported to `@eks.flow` with strict `expect` and no sleeps, QA suites as tagged flow groups |

Both repos' `.claude` skills, agents and CLAUDE.md harness sections are rewritten for the
new API. Done when all 174 flows pass against the migrated backend and seed.

## 17. Later decisions

| Topic | Decision |
| --- | --- |
| Backend clock | Frozen per run through an injectable clock in eksenapi, enabled on local backends only (phase 2 adds the eksenapi side; the engine passes plan `now` to the backend lease). |
| gfx renderer | GPU (Skia GL or wgpu). Determinism guard: the GPU adapter and driver are part of the cache key, a gfx layer renders on one machine per clip, and a CPU reference path (`--cpu`) exists for goldens and visual checks. |
| Studio | Rebuilt on the plan IR as the media view of plans and runs; one job model (runs). |
| Order | Strictly M0 to M7; phase 2 starts after M7. |
