# eks-harness overview

eks-harness drives web and mobile apps under test, seeds their backends, keeps every capture as evidence and
renders videos from it. Everything it does is described in one Python API, `eks`: a function decorated with
`@eks.flow`, `@eks.seed`, `@eks.edit`, `@eks.scene` or `@eks.score` is traced into a typed plan, and the hub runs
the plan. This page describes the parts and how they fit. The reference pages under [reference/](reference/) list
every op, catalog entry, check, preset and CLI option; [design](dsl/design.md) records the decisions behind the API.

## Parts

```text
 authoring (client, no IO)          hub (daemon)                              executors
 ------------------------          ----------------------------------         -------------------------
 import eks                        /api/plans  build and validate             web worker   (typed op RPC)
 @eks.flow/seed/edit/scene/score   /api/runs   schedule, journal, events      mobile worker + RN bridge
 builders -> Ref, Time, Target ->  engine      resources: leases, backends,   native surface (adb, idb)
 plan (JSON, Merkle hashed)                    seeds, cassettes, render slots api and seed resources
                                               cache (content addressed)      media: edit, gfx, audio,
                                               debugger, dynamic sandbox        overlays, media checks
```

| Part | Package or path | Role |
| --- | --- | --- |
| Authoring SDK | `eks` | decorators, builders, `Ref`, `Time`, `Target`, `by`, `expect`, control flow; imports nothing heavy and does no IO |
| Plan IR | `eks_harness.ir` | pydantic models of plans, op argument models, catalogs, presets, the registry and the JSON Schema |
| Engine | `eks_harness.engine` | validator, admission, resource planner, scheduler, journal, cache, debugger, dynamic sandbox, run manager |
| Executors | `eks_harness.ops`, `eks_harness.drivers`, `eks_harness.data`, `eks_harness.media` | what each op does: API calls, driver ops, seed resources, media renders and checks |
| Hub | `eks_harness.daemon`, `eks_harness.api` | the daemon: HTTP API, web UI, MCP over HTTP, artifact store, pools, leases, backends, nodes |
| Workers | `workers/kit`, `workers/web-driver`, `workers/mobile-driver` | Node processes that run typed ops on a leased browser or device ([drivers.md](drivers.md)) |
| React Native bridge | `sdk/react-native` | runs inside dev builds: targets, actions, store, overlays, network shim, fakes |
| CLI | `eks_harness.cli` | `eks-harness` (alias `ehx`) |
| MCP | `eks_harness.mcp_engine` and the other `mcp_*` modules | tools for agents, over stdio (`eks-harness mcp`) and HTTP |
| Claude Code plugin | `claude/` | skills, agents, commands and hooks built on the CLI and MCP tools |

## Plans and runs

An entry file lives under `.harness/{flows,seeds,edits,scenes,scores}/` of a tree, or under
`<app>/.harness/flows/` for flows of one app. Tracing an entry runs the function with builder objects and records
every call as a node: `s.ui.tap("#save")` becomes a `ui.tap` node, `s.api.get(...)` an `api.get` node whose output
later nodes can read through a Ref. Tracing does no IO, so a plan can be built, validated, diffed and hashed
without a browser or backend.

```bash
eks-harness plan .harness/flows/subeler.py            # build and validate, print the nodes
eks-harness plan .harness/flows/subeler.py --json     # the plan JSON and the validation report
eks-harness run .harness/flows/subeler.py             # run it on the hub and follow it
eks-harness run .harness/flows/checks.py::login --set user=admin --platform ios
```

`eks-harness run` builds the plan locally, validates it, sends it to the hub (`POST /api/runs`) and streams node
outcomes until the run ends. The hub admits the plan (validation, executor and resource checks), acquires its
resources, runs the nodes and writes every node instance, event and artifact to the run journal (SQLite). A run
ends `passed`, `failed`, `error` or `aborted` with one report: failures with their node, source line and failure
shot, checkpoints, artifacts with links, values and timings. The CLI, MCP and HTTP API render the same report.
`eks-harness runs list|show|replay|control|cancel` work on earlier runs.

Exit codes of `run`, `runs` and `debug`: 0 passed, 1 failed, errored or invalid, 12 paused without a terminal
(attach with `eks-harness debug <run>`), 13 aborted. `--repeat N` runs the plan N times, links the runs and
reports nodes whose results differ.

Plans are deterministic by construction: node ids come from the source, hashes are Merkle hashes over the
canonical JSON, the clock is frozen and every random seed derives from one root seed. [determinism.md](determinism.md)
states what each executor guarantees; [reference/ir.md](reference/ir.md) describes the plan format.

### Control flow and values

Ops return lazy Refs. A Ref can be indexed, filtered (`items.find(name="Kadıköy")`, `.filter(...)`, `.first`,
`.last`, `.len()`) and interpolated into strings, and it is resolved when the node that reads it runs. Branches
and loops over run-time values go through `eks.when` / `eks.otherwise`, `eks.each` and `eks.parallel`;
`@eks.dynamic` runs a function in a sandboxed subprocess on the hub with the real values and returns a sub-plan.
`bool(ref)` raises, so a Python `if` on a run-time value fails at plan time with a pointer to `eks.when`.

### Time and targets

Every duration is a `Time`: `t("1.5s")`, `t("300ms")`, `t("f:120")`, `t("beat:8") + t("1b")`, `t("bar:4")`,
`t("cue:logo")`, `t("marker:intro#2")`, `t("word:'EksenGOLD'")`. Bare numbers are rejected. Times are integer
ticks (705 600 000 per second), so frame times are exact at every common frame rate.

Elements are named with `by.test_id`, `by.role`, `by.text`, `by.label`, `by.placeholder`, `by.css` (web),
`by.kind` (React Native), `by.point` and `by.rect`, refined with `.within(...)`, `.near(...)`, `.nth(i)`,
`.first` and `.last`, or with the shorthand strings `"#test-id"`, `"role=button:Kaydet"` and plain text.
Matching is strict: no match waits until the timeout, more than one match fails with the candidates listed.
`.native` on a target reaches system dialogs outside the app on iOS and Android.

### Debugging

`s.debug.break_("label")` in the source, or `eks-harness run ... --break op:ui.tap --break-on-error`, pauses the
run. A paused run shows the node, its resolved arguments, a screen and DOM snapshot and the Refs computed so far;
the debugger can continue, step, retry, skip, edit the arguments and resume, abort, or run an ad-hoc op in the
paused session. The same controls are `eks-harness debug <run>`, `eks-harness runs control <run> <action>` and the
MCP tool `eks_run_control`.

## Sessions and surfaces of a flow

A flow drives one app on one platform (`@eks.flow(app="web-app")`, or `platforms=[...]` for a matrix and
`--platform` to pick one). Its session `s` has these namespaces:

| Surface | Ops |
| --- | --- |
| `s.ui` | platform-neutral navigation, input, reads, waits (`nav`, `tap`, `fill`, `text`, `wait_for`, `settle`, ...) |
| `s.web`, `s.mobile` | web specifics (`viewport`, `logs`, `wheel`) and React Native specifics (`dispatch`, `state` reads, `patch`, `mount`, `idle`, ...) |
| `expect(...)` | assertions on targets and Refs; `expect.soft` and `expect.eventually` |
| `s.checkpoint`, `s.shot`, `s.dump`, `s.har`, `s.record`, `s.hold` | evidence and recordings |
| `s.overlay` | live overlays drawn into the app (title, caption, callout, spotlight, highlight, arrow, blur, redact, icon, image, step) |
| `s.api`, `s.as_`, `s.backend.info` | backend calls as a persona, persona logins |
| `s.net`, `s.inject`, `s.eval` | route mocking, network shaping, cassettes, script and style injection |
| `s.fake` | fake camera, NFC and document inputs the app registers |
| `s.debug` | breakpoints |

[reference/ops.md](reference/ops.md) lists every op with its Python call, platforms and arguments. App runtimes
(`@eks.app(name)` classes with `prepare`, `boot` and `login` hooks) start dev servers, install and launch apps and
log personas in; without one, the app profile's `[web]` and `[mobile]` settings are used.

A flow holds its browser or device lease for the whole run. It holds a backend only when it needs one: when it
names `seed=`, `backend=` or `as_`, or uses an op with the backend role (`api.*`, `session.as`, `backend.info`,
`data.*`). A UI-only flow runs with no backend.

## Pools and leases

The hub owns a pool of browser profiles in shared Chrome instances, iOS simulators and Android emulators, and
lends them out as leases. A driver lease is exclusive and short, has a six-character sid and waits in a FIFO queue
when the pool is busy. Runs take and release their leases themselves; for manual work there is
`eks-harness lease acquire --kind browser|ios|android` and `lease release`. Captures made on a lease go to the
project and session of the lease.

## Backends

A backend is a service stack an app talks to, started, health-checked, stopped and cleaned up by the hub. A
definition is an `@eks.backend(name, requires=, identity=, idle_timeout=)` class (or a legacy plugin backend)
with hooks such as `prepare`, `spec` or `start`, `health`, `info`, `stop` and `cleanup`. An instance is keyed by
its definition and identity (`tree` by default, or `host` or `global` for shared infrastructure). Holders take
shared backend leases (`bl_...`, `eks-harness backend acquire`); the first acquire starts the instance and the
last release starts its idle timeout, after which the hub stops it and runs its cleanup.

`requires=` names other backend definitions. Before a dependent instance starts, the hub takes a dependency lease
on each requirement, waits until it is ready and passes its info to every hook of the dependent as
`context["dependencies"][name]`. A dependency stops only after its last dependent, following its own idle
timeout.

## Seeds

A seed is a resource graph: `@eks.seed(name, backend=, now=, seed=)` declares resources of kinds that projects and
plugins register with `@eks.resource`. The seed engine creates them in phase and program order through each kind's
`create(self, ctx)` hook, snapshots the backend, and restores the snapshot into later backend leases. Snapshot keys
cover the seed plan hash and the backend's migrations, so a schema change or a seed edit builds a new snapshot.
Persona kinds produce the logins flows use with `as_=`. `eks-harness seed snapshots|show|drop` and the MCP tools
`eks_seeds` and `eks_seed_show` list them. [reference/resources.md](reference/resources.md) has the hook contract.

## Media

Edits, gfx scenes and audio mixes are plans too. `@eks.edit` builds a timeline of tracks and clips with effects,
transitions and video overlays; `@eks.scene(kind="gfx")` builds 2D motion graphics; `e.audio` builds a mix graph
with ducking, compression and a loudness target. The edit renderer is a frame compositor with bitexact ffmpeg
encoding, so the same plan renders the same bytes twice on one machine. `eks.annotate(shot, [...])` draws
overlays on a screenshot, and `e.expect.derive(...)` turns an edit's cuts, captions, beats and loudness target
into media checks. Blender and web scenes and scores with pinned device takes are traced into plans today; their
executors arrive with the scene and score milestone, and until then the legacy video, studio and score tools
(`eks-harness video`, `studio`, `score`) render them.

## Store

Every capture is an artifact in a project (`owner/name`) and a session (usually the branch): screenshots,
videos, DOM, MHTML and accessibility dumps, HARs, logs and renders, each with tags, retention, notes, versions and
share links. `eks-harness artifacts`, `projects`, `sessions`, `share` and `search`, the store MCP tools and the web
UI work on it. Run reports link to the artifacts they made.

## Nodes

A node is another machine that dials the hub and runs jobs in CPU and GPU slots (renders, previews, builds).
`eks-harness node add <ssh-host>` installs the node agent; `eks-harness node` and `job` list and manage them.

## Extending

Projects and plugins extend the registry with Python decorators: `@eks.op` (a new op with its argument model and
executor), `@eks.macro` (a composite that appears as `s.<namespace>.<name>`), `@eks.resource`, `@eks.check`,
`@eks.effect`, `@eks.transition`, `@eks.easing`, `@eks.curve`, `@eks.marker`, `@eks.overlay_kind`,
`@eks.target_kind`, `@eks.renderer`, `@eks.backend`, `@eks.backend_policy`, `@eks.app` and `@eks.preset`. Worker
ops in JavaScript register with `defineOp` in a driver extension. A plugin is a folder with `harness-plugin.toml`
(`[plugin]` identity, `modules = [...]` to import, requirements and settings); repo plugins under
`.harness/plugins/` load after they are trusted. [docs/multi-repo.md](multi-repo.md) covers trust and module
isolation across repositories and worktrees.

## Surfaces

| Surface | Use |
| --- | --- |
| CLI | `plan`, `run`, `runs`, `debug`, `do`, `ops`, `schema`, `seed`, `docs`, plus the hub, store, lease, backend, node and plugin commands ([reference/cli.md](reference/cli.md)) |
| MCP | `eks_run`, `eks_plan`, `eks_validate`, `eks_ops`, `eks_do`, `eks_run_status`, `eks_run_control`, the flow file tools `eks_flows`, `eks_flow_get`, `eks_flow_save`, `eks_flow_delete`, interactive sessions `eks_session_open`, `eks_session_do`, `eks_session_close`, `eks_session_export`, seeds `eks_seeds`, `eks_seed_show`, and the store, lease, backend and system tools |
| HTTP | `/api/plans`, `/api/runs` (create, status, report, plan, journal, events, control, cancel, replay), `/api/ops`, `/api/flows`, `/api/seeds`, and the store, pool and backend routes |
| Web UI | projects, sessions and evidence, devices and browsers, backends, nodes, plugins and the studio |

An agent can work without writing a file first: `eks_do` runs one op as a one-node plan, and
`eks_session_open` starts a paused debug session in which `eks_session_do` runs ops one by one;
`eks_session_export` turns the ops of a session into a `.py` flow.
