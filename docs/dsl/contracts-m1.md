# eks M1 contracts: engine and hub

Status: normative for milestone M1 (design.md section 16: "a plan with api and fake executors runs end to end in
tests"). Sources above this file: `docs/dsl/design.md` (sections 3, 5, 6, 9, 12, 16), `contracts-m0.md`,
`decisions-m0.md`, `backlog.md` (every M1 row is a requirement here, section 22). Choices the design left open are
recorded in `decisions-m1.md`; packages report new ones in their results (section 24).

The skeleton modules of section 1 are part of this contract: every class, field, default, signature, constant and
`__all__` in them is normative. Where a skeleton body raises `NotImplementedError`, the owning package implements it
exactly as specified here. A package that finds a contract genuinely wrong fixes it in this file and in the code in the
same change and says so in its result; it never edits files another package owns (section 24) and never adds a fallback
for something another package has not implemented yet.

## 0. Ground rules for every package

* Everything of contracts-m0.md section 0 applies: Python 3.13, pydantic 2.14, ruff 0.16 default rules clean, pyright
  1.1.390 clean (`cd <worktree> && npx --yes pyright@1.1.390 --pythonpath .venv/bin/python <paths>`), no comments, no
  docstrings, no `# noqa`, no em dash, no TODO, no `NotImplementedError` left in non-test code, human text in
  `description=` fields and error messages. Never commit, push, branch or create worktrees.
* New FastAPI routes use `Annotated[..., Depends(...)]` and `Annotated[..., Query(...)]` parameters (no calls in
  defaults; ruff B008), as the skeleton routes do.
* Threads, not asyncio, run the engine: the platform's database connections are per thread (`Database.conn()`), leases
  and backends are thread based, and executors are synchronous callables. Async code is limited to route handlers and
  the event stream; blocking calls from async routes go through `starlette.concurrency.run_in_threadpool`.
* Engine modules may import `eks` (the SDK) and `eks_harness.ir`; `eks_harness.ir` never imports the engine;
  `eks_harness/engine/__init__.py` keeps exporting the validator only (importing `eks` from it would create the cycle
  `eks.workspace -> eks_harness.engine -> eks`).
* Tests live in `tests/engine/` (no `__init__.py`, no `conftest.py`, basenames unique across `tests/`, prefix
  `test_engine_`). The fixtures of `tests/conftest.py` (`harness_home`, `paths`, `config`, `app`, `ctx`, `db`,
  `client`, `auth_env`, `harness_client`) are available; the `eks_registry` autouse fixture covers only `tests/eks`, so
  every engine test module that registers ops, entries, executors or plugins snapshots and restores the process
  registry with its own module-level autouse fixture (as `tests/engine/test_m0_authoring_residuals.py` does) and builds
  trees under `tmp_path`. Run: `cd <worktree> && EKS_HARNESS_SKIP_WEB=1 uv run pytest -q -p no:cacheprovider
  tests/engine/<files>`.
* Old APIs keep working until M7: no existing route, CLI command, MCP tool, flow runner or test may break.

## 1. Package layout and module map

| Module | Role | Owner |
| --- | --- | --- |
| `engine/model.py` | every shared model, literal and handle: run and node states, options, requests, records, report, control, handles | P1 |
| `engine/errors.py` | exceptions and the error code table `ERROR_CODES` | P1 |
| `engine/values.py` | instance keys, `ValueStore`, run-time resolution of `$ref`/`$item`/`$fmt`, argument validation, output type checks (BL-006), truthiness, value digests | P1 |
| `engine/debugger.py` | breakpoint grammar, condition evaluator, debugger state machine, control tickets | P1 |
| `engine/journal.py` | SQLite journal of runs, node instances, events, expansions and resources; `RunJournal` writer | P2 |
| `engine/events.py` | run events on the daemon `EventBus`, the server-sent event stream of a run | P2 |
| `engine/report.py` | run summary, report, journal page and run comparison built from the journal | P2 |
| `db/migrations/0007_engine.sql` | journal and cache index tables | P2 |
| `engine/drivers.py` | `DriverPort`/`DriverProvider` protocols, driver registry, per-run ports | P3 |
| `engine/executors.py` | executor protocol, `ExecutorSpec`, `ExecutorRegistry`, `ExecContext`, artifact sink and store adapter | P3 |
| `ops/fakes.py` | fake executors, fake driver ports, fake seed and cassette hooks | P3 |
| `engine/cache.py` | cache keys, content resolution, resolution requirements, content-addressed store | P4 |
| `ir/hashing.py`, `ir/registry.py`, `ir/catalog/effects.py` | BL-008 device-dependent effects, public `pure_closure` and `node_needs` | P4 |
| `engine/registries.py` | hub tree registries (BL-007), plugin and tree code digests (BL-004) | P5 |
| `eks/discovery.py`, `eks/workspace.py` | isolated tree registries, plugin and file pruning, bounded caches (BL-007) | P5 |
| `engine/resources.py` | resource planner: driver leases, backend leases (BL-005), seed and cassette hooks, render slots | P6 |
| `engine/auth.py` | persona auth hook for `api.*` | P6 |
| `renderq.py` | thread-safe `claim`/`drop` render slot API | P6 |
| `engine/sandbox.py`, `engine/sandbox_child.py` | dynamic sandbox: parent runner and child process | P7 |
| `ops/__init__.py`, `ops/api.py`, `ops/session.py`, `ops/expect.py`, `ops/worker.py` | real executors: `api.*`, `backend.info`, `session.as`, `expect.value`, worker routing | P8 |
| `engine/build.py`, `engine/admission.py` | hub-side planning (spec, inline source, one-shot), validation, run admission | P9 |
| `engine/scheduler.py` | the DAG scheduler: lanes, containers, expansions, cache, debugger, retries | P10 |
| `engine/runs.py`, `api/routes_{plans,runs,ops,flows}.py`, `api/schemas_engine.py`, `daemon/app.py`, `config.py` | run manager service, HTTP API, settings | P11 |
| `cli/run_cmds.py`, `cli/__init__.py`, `mcp_engine.py`, `mcp_server.py` | CLI `run`, `runs`, `debug`, `do`; MCP tools | P12 |

The architect already wired the skeleton: `daemon/app.py` `ROUTER_MODULES` holds the four route modules,
`cli/__init__.py` `COMMAND_MODULES` holds `run_cmds`, `mcp_server.create_server` calls `register_engine_tools`, and
`config.py` holds the `engine.*` settings (section 21). Paths in this file are relative to `src/eks_harness/` unless they
start with `src/`, `tests/` or `docs/`.

## 2. Runs: lifecycle and states

### 2.1 Run states

`RunStatus`: `queued`, `preparing`, `running`, `paused`, `passed`, `failed`, `aborted`, `error`. Terminal:
`TERMINAL_STATUSES` (`passed`, `failed`, `aborted`, `error`).

| From | To | When |
| --- | --- | --- |
| (none) | `queued` | admission accepted the run and the journal row exists |
| `queued` | `preparing` | the run manager dispatches it (at most `engine.maxRuns` runs are `preparing`, `running` or `paused`) |
| `queued` | `aborted` | cancel before dispatch; reason `cancelled before it started` |
| `preparing` | `running` | every resource is held (section 10) |
| `preparing` | `error` | a resource failed; reason `resource_error: <message>` |
| `preparing` | `aborted` | cancel during preparation |
| `running` | `paused` | the debugger paused (section 13) |
| `paused` | `running` | continue, step, retry, skip |
| `running`, `paused` | `passed` | every node instance of the root scope is settled and none failed hard |
| `running`, `paused` | `failed` | a node instance failed hard and was not paused (reason `<op> <node> failed: <message>`) |
| `running`, `paused` | `aborted` | abort control, cancel, or a pause longer than `engine.pauseTimeoutSeconds` |
| any non-terminal | `error` | an engine invariant broke (reason `engine: <message>`), the hub shut down (`the hub shut down while the run was active`) or the hub restarted (`RECOVERY_REASON`, set by `RunManager.recover`) |

Every transition is journaled (`run.<status>` event, section 7.3) and published (section 8). `runs.started_at` is set
when the run enters `running` the first time, `finished_at` when it becomes terminal. Resources are released on every
terminal transition (section 10.6). Who writes them: the run manager journals `queued` (right after `create_run`),
`preparing`, `running` (after `planner.acquire` returned, before `Scheduler.run()`), every terminal status (from the
`RunOutcome`, after resources are released) and `error`/`aborted` before the scheduler started; the scheduler journals
only `paused` and the `paused` -> `running` resume through `deps.journal.status`.

### 2.2 Node instances and states

A node instance is one execution unit: `(node id, instance key)`. The instance key names the runtime expansions that
enclose it (`values.py`):

```text
instance ::= "" | segment ( "/" segment )*
segment  ::= EACH_ID "#" INDEX        an iteration of a ctl.each node (INDEX from 0)
           | DYNAMIC_ID               the sub-plan of a ctl.dynamic node
```

`child_instance(parent, segment)` joins with `/` (no leading `/` for the first segment), `each_segment(id, i)` is
`f"{id}#{i}"`, `dynamic_segment(id)` is `id`, `instance_segments` parses (`Segment(node_id, index)`, index `None` for a
dynamic segment) and `instance_prefixes(instance)` lists the instance and each shorter prefix down to `""`, longest
first. Root nodes and nodes inside `when`, `parallel`, `macro`, `record`, `cassette` and `take` bodies share their
container's instance.

`NodeStatus`: `pending` (instantiated, waiting), `ready` (dependencies settled, waiting for a slot), `running`, `paused`
(the run paused at it), `passed`, `soft_failed` (failed without failing the run), `failed`, `skipped` (untaken branch,
debugger skip), `cancelled` (the run ended before it ran or while it ran). `SETTLED_STATUSES` (`passed`,
`soft_failed`, `skipped`) satisfy dependencies; `FINAL_NODE_STATUSES` add `failed` and `cancelled`. `seq` is a per-run
counter assigned when an instance is first created; the root scope's nodes are instantiated at run start in plan walk
order, a branch's nodes when the branch activates (section 5.4).

### 2.3 Counts and reasons

`RunSummary.counts` maps every `NodeStatus` that occurs to its number of instances (containers included).
`CacheCounts` counts pure node instances by outcome (section 9.6). Reasons are short single-line strings; the report
(section 14) carries the structured failure.

## 3. Admission (P9, `engine/admission.py`)

`Admission.admit(request, *, run_id, created_at, actor, user_id) -> AdmittedRun` runs synchronously inside
`POST /api/runs` (and `replay`, `do`). It never acquires resources and never starts executors. It raises
`AdmissionError(message, issues)` (HTTP 422, section 15) or `BuildError` (HTTP 400). Steps:

1. Plan. Exactly one of `request.plan`, `request.spec`, `request.source` (else `AdmissionError`, code `plan.source`).
   A spec or source is built with `PlanBuilder.build` (section 16.1). A plan JSON is taken as given (the route parses
   the raw JSON with `Plan.model_validate` and refuses with a `plan.json` issue when it does not parse).
2. Registry. `entry = builder.registries.for_tree(tree, plugins=request.options.plugins)` where `tree` is the built
   plan's tree, else `Path(request.tree)` (must be an existing directory, else `plan.tree`), else `None`. A plan whose
   `requires` is not empty, that uses a backend resource, or that holds a relative `file:` media source needs a tree
   (`plan.tree`).
3. Validation. `eks.validation.validate(plan, registry=entry.registry, tree=tree)` (contracts-m0 section 11) plus
   `HubRegistries.requirement_issues(plan, entry)` (section 16.2). Errors refuse; warnings are kept as
   `AdmittedRun.issues` and journaled. The validator's `plan.hash` rule guarantees the plan JSON was not edited after
   hashing; `plugin.missing`/`plugin.version` compare `plan.requires` with the tree registry (BL-004, BL-007).
4. Clock (BL-010). `clock_issues(plan, registry, request.clock)`: when `plan.kind == "scene"` and
   `plan.clock.tempo` is external (`TempoMap.external`), every node is checked with `ir.hashing.node_needs(node,
   registry)` (section 9.9); a node needing `tempo`, `downbeat`, `cues` or `markers` without a run clock supplying them
   (`RunClock.tempo` with a `bpm`, the needed cue names, the needed marker streams) is the error `tempo.external`
   ("scene <name> uses <what> but its tempo is external; pass a run clock with bpm, cues and markers, or embed the
   scene in a score"). A run clock on a plan whose tempo is not external is the error `tempo.external` too ("the plan
   has its own clock").
5. Fakes. With `request.fakes`, `fake_issues(profile, registry)` reports every `families`/`force` pattern that matches
   no registered op (`fakes.pattern`, error), and `FakeExecutors(profile, registry)` becomes the fake source.
6. Executors. `choose_executors(plan, registry, fakes, fake_source)` walks every node (all branches) and maps each op
   name to an `ExecutorChoice` (section 4.4). An op without a choice is `executor.missing` ("no executor for <op> on
   this hub; driver executors arrive in M2, seed executors in M3, media executors in M4 and M5; run with fakes to
   simulate it"). A plugin op that names a Python `executor=` while its `location` is not `hub` is `executor.location`
   (worker plugin ops without `executor=` go through the worker executor). Every lease resource needs a
   driver provider for its platform: `drivers.provider(platform)` or, with `fakes.drivers`, the fake one; else
   `driver.missing`.
7. Resources. `planner.plan(...)` (section 10.1) and `resource_issues`: for a `lease.ios`/`lease.android` resource
   `devices(kind)` (the pool's `available`) false is `lease.kind`; a backend resource with a seed while
   `options.seed == "restore"` and neither `seeds.hook(definition)` nor `fakes.seeds` is `seed.hook` (the definition is
   only known for `plan` and `request` sources; others are checked again at acquisition); a `net.cassette` node in
   `replay` mode whose cassette is missing is `cassette.missing` when the hook can tell before the backend is known
   (key without backend) and is checked again at acquisition.
8. Target. `target(request, tree)`: project = `request.project`, else the tree's `[project] id`
   (`plugins.project.load_project_config(tree).project_id`), else `target.project` error; session = `request.session`,
   else `pools.backend_info.git_facts(str(tree))["branch"]` when it has one, else `DEFAULT_SESSION`. Authorization is
   the route's job (section 15.1).
9. Result. `AdmittedRun(info, plan, registry=entry, executors, resources, sessions=model.sessions_of(plan), request,
   environment, issues, fakes, simulated)` where `info` is the `RunInfo` with the run id, target, options, fakes and
   clock, `simulated` is true when any choice is fake or fake drivers, seeds or cassettes are in use, and
   `environment(entry, executors)` fills `EnvironmentOut` (harness version, `cache.harness_digest()`, Python version,
   `platform.system().lower() + "-" + platform.machine().lower()` normalized like node platform tags, executor identity
   per op, every loaded plugin's version and digest, the project digest, the faked ops).

`ENGINE_ISSUE_CODES` lists the engine's own codes; every refusal carries the validator issues and the engine issues as
`IssueOut` in walk order, plan-level first.

## 4. Executors (P3 `engine/executors.py`, `engine/drivers.py`)

### 4.1 Protocol

An executor is an `ExecutorSpec`: `name`, `version` (never empty), `ops` (patterns, 4.3), and either `run`
(`Callable[[ExecContext], ExecResult]`) for plain ops or `container` (`enter`/`exit`) for container ops; optional
`resolver` (4.6), `locations`, `runtime_version`, `fonts`, `fake`, `provenance`, `description`. `identity` is
`name@version`. Executors are synchronous, run on a scheduler worker thread, honour `call.cancel` and `call.remaining()`
for every blocking wait, and report expected failures by raising `NodeFailure(code, message, detail=, retryable=,
soft=)` (`errors.py`); any other exception becomes `executor_error` with the exception type, message and the last 20
traceback lines in `detail`. `as_result(value)` turns a plain return value of a plugin executor into `ExecResult(value=)`
(an `ExecResult` passes through).

`ExecResult`: `value` (JSON, checked against `node.out`, BL-006), `content` (a content digest override for cache inputs,
9.3), `artifacts` (ids in addition to those the sink recorded), `resolution` (the machine resolution the executor
actually used, 9.4), `soft` (soft failures recorded with a value: the instance ends `soft_failed`), `detail` (journaled
with `node.passed`).

Container executors: `enter(call) -> ContainerEntry(branches, detail)` decides which branches run (`("body",)` for
record, cassette and take); after the body, `exit(call, BodyOutcome(status, branches, values)) -> ExecResult` produces
the container's output (`values` maps body node ids of this instance to their outputs).

### 4.2 Execution context

`ExecContext` (constructor in the skeleton) gives an executor: `run` (`RunInfo`), `plan`, `registry` (the run's tree
registry), `node`, `spec` (`OpSpec`), `op`, `instance`, `attempt` (1 for the first execution), `args` (the op's argument
model, validated, every ref resolved), `data` (the resolved JSON args by alias), `session` (the `SessionSpec` governing
the node: the take's inside a take, the plan's otherwise, `None` without one), `state` (`SessionState` of that session,
shared by the session's nodes during the run), `resources` (`ResourceView`: handles by plan resource id), `driver` (the
`DriverPort` of the node's lease, or `None`), `artifacts` (`ArtifactSink`), `seed` (`ir.hashing.node_seed(plan.clock.seed,
node)`), `clock` (the plan clock; with a run clock, `tempo` is the run clock's tempo and the run clock's cues are added
to the plan's; run clock markers are read from `call.run.clock`), `deadline` (monotonic seconds or `None`), `cancel` (the
run's cancel event), `simulated` (the executor is fake or the node's driver port is simulated).

Methods: `remaining()` seconds to the deadline (`None` without one, never negative); `check_cancelled()` raises
`NodeFailure("cancelled", ...)` when `cancel` is set or the deadline passed (`timeout`); `log(message, level=, detail=)`
journals `node.log`; `event(type, detail)` journals `node.<type>` when `type` has no dot, else `type` as given;
`progress(fraction, message)` publishes live progress through the sink (`RunJournal.progress`, not journaled; calls
less than 0.25 s after the previous delivered one are dropped unless `fraction >= 1.0`); `lease()` returns
the handle of the node's `lease.*` resource (from `node.uses`) or raises `NodeFailure("resource_error")`; `backend()` the
same for the backend resource; `reread(expr)` re-executes the idempotent producer of a `$ref` expression and returns the
fresh resolved value (5.10), raising `NodeFailure("assertion")` with a clear message when `expr` is not a `$ref` or its
producer is not idempotent.

### 4.3 Matching

`pattern_matches(pattern, op) -> int`: `MATCH_EXACT` (3) when `pattern == op.name`; `MATCH_FAMILY` (2) when `pattern ==
op.family` or `pattern == op.family + ".*"`; `MATCH_ANY` (1) for `"*"`; else `NO_MATCH`. `ExecutorSpec.match(op)` is the
best pattern of `ops`, and `NO_MATCH` when `locations` is set and does not contain `op.location`, when `op.container`
is true and the spec has no `container`, or when `op.container` is false and the spec has no `run`.
`FakeProfile.families` and `force` use the same patterns (`ops.fakes.covers`).

### 4.4 Registry and choice

`ExecutorRegistry.register(spec)` refuses (`ValueError`) an empty name or version, a spec with neither or both of `run`
and `container`, and a second spec with the same name and an overlapping exact pattern. `real(op)` returns the
registered spec with the highest match for `op` (ties: the later registration), else `plugin_executor(op)`. An op's own
`executor` reference beats family and `"*"` matches: when `op.executor` is set and no registered spec matches `op`
exactly, `real(op)` returns `plugin_executor(op)` (a worker-location plugin op never falls to `eks.worker`).

`plugin_executor(op)`: when `op.executor` is set (`"module:qualname"`, contract-m0 8.2), the module is imported
(`importlib.import_module`; scoped plugin module names are already in `sys.modules` after the tree registry loaded the
plugin) and the qualname resolved attribute by attribute. A class is instantiated with no arguments; the result (or a
function) must be callable with one `ExecContext`; it is wrapped so its return value goes through `as_result`. The spec
is `ExecutorSpec(name=f"plugin:{op.provenance.plugin}:{op.name}", version=op.executor_version or op.provenance.version
or "0", ops=(op.name,), run=..., provenance=op.provenance)`; container plugin ops must resolve to an object with `enter`
and `exit`. Resolution failures raise `ValueError` with the reference and the cause; admission reports them as
`executor.missing` with that message.

`choose(op, fakes=, fake_source=) -> ExecutorChoice | None`:

1. `op.name in NATIVE_OPS` (`ctl.*`, `debug.break`, `debug.pause_on_error`): `ExecutorChoice(native_executor(),
   native=True)`, where `native_executor()` is `ExecutorSpec(NATIVE_EXECUTOR, NATIVE_VERSION, ops=tuple(NATIVE_OPS),
   run=<a function raising NodeFailure("executor_error", "native ops run in the scheduler")>)`.
2. With `fakes` and `fake_source`: when a `fakes.force` pattern covers the op, the fake spec (`fake=True`).
3. `real(op)`: the real spec.
4. With `fakes` and `fake_source`: when a `fakes.families` pattern covers the op, the fake spec.
5. `None`.

`reason` says which rule chose it. The worker executor (P8) is registered twice, as `eks.worker` (`run`) and
`eks.worker.container` (`container`), both with `ops=("*",)` and `locations=frozenset({"worker"})`, so every worker op
and worker container is "real" when it has no more specific executor; it needs a driver port at run time (admission
checks providers, 3.6).

### 4.5 Locations

`OpSpec.location` is where the op's work happens: `hub` (in the hub process), `worker` (a driver worker reached through
the lease's `DriverPort`), `node`, `blender`, `browser_scene` (render nodes). In M1 every executor body runs on a hub
thread; `worker` executors talk to the port, and `node`/`blender`/`browser_scene` executors (fakes in M1) run inside a
render slot (section 10.5). The journal records `op.location` per instance. Real worker, render and seed executors are
M2 to M5; they register through the same registry without engine changes.

### 4.6 Resolvers and versions (BL-001, BL-003, BL-008, BL-009)

A machine-dependent op's executor (an op whose nodes can be machine-dependent: `media.markers`, `check.media`,
`edit.render`, `gfx.render`, `blender.render`, `scene.render`, `audio.mix`, every `device_dependent` op) has a
`resolver`: `resolver(ResolveContext) -> Mapping[str, JsonValue]` predicts, without doing the work, what this machine
will use (beat backend and version, speech model, device, compute type and backend version, OCR engine, encoder and its
version, GPU adapter and driver, worker platform tag, browser build, inference device, model and weights). A pinned
choice that is unavailable raises `NodeFailure("resolution_unavailable")`; there is never a fallback. The fields a node
needs are `cache.required_resolution(node, registry)` (9.4); the executor must report at least those, and its
`ExecResult.resolution` must agree with the prediction on them (`resolution_mismatch` otherwise).

`version` is the executor's own version; `runtime_version()` adds what only exists at run time (a Blender, Cycles and
OIDN version string, a worker protocol and driver version); `fonts=True` marks text-rendering executors (BL-003); a
built-in executor (`provenance.plugin == "eks"`) is also keyed by the harness source digest (9.2). All four enter cache
keys; none enters node hashes (`OpSpec.executor_version` stays the plan-time value, decision row "Executor versions").

### 4.7 Artifacts and session state

`DaemonArtifacts(db, config, events)` implements `ArtifactStore` over the daemon store: `ingest` calls
`store.artifacts.ingest_file(db, config, project=, session=, path_or_stream=path, kind=, caption=, tags=, meta=,
source="agent", user=actor, filename=name, events=, move=)` and converts the repo row to `ir.types.Artifact` with
`Links(config)` (`url` = `links.artifact`, `raw_url` = `links.raw`, `session_url` = `links.session`, `mime`, `size`,
`sha256` as `sha256:<hex>`); `get` and `file` read the repo row and the stored file; `scratch()` is a fresh directory
under `paths.tmp_dir / "engine"`.

`ArtifactSink.ingest` adds the tags `run:<run id>`, `node:<node id>` and the op name and the meta `{run, node,
instance, op}`, ingests into the run's project and session, records the id in `produced`, calls `sink.artifact(...)`
(which journals `node.artifact`), and returns the `Artifact`. `write(data, filename=, ...)` writes the bytes into a
scratch file and ingests it with `move=True`.

`SessionState(key, persona, values)`: one per session key per run, created by the scheduler; `persona` starts as the
session's `persona` and `session.as` changes it (section 20).

### 4.8 Drivers

`DriverPort.call(op, args, site=, phase=, timeout=) -> DriverReply(value, files, detail)` runs one worker op (`phase`
`run`, or `enter`/`exit` for worker containers); `site` is the `CallSite(node, instance, attempt)` of the node instance
the call serves (ports name evidence and step logs by it; the fake port derives its answers and bytes from it); `files` are `DriverFile(path, role, kind, name, caption)` the executor ingests:
role `value` makes the ingested artifact the node's output (an `Artifact` for `out == "artifact"`, a `MediaInfo`
`{uri: "artifact:<id>", kind, artifact: <id>, ...reply value fields}` for `out == "media"`), role `shot` appends the id to
`value["shots"]` (checkpoints), role `evidence` only records it. `snapshot(kinds, dest)` writes screen, DOM or
accessibility snapshots for failure shots and pauses. `simulated` is true for ports that do not drive a real device or
browser (the fake port). `DriverRegistry` maps platforms to providers (a later
registration of a platform replaces the earlier one); `DriverPorts(providers, run, sessions).port(lease)` opens one port
per lease resource id lazily (the provider of `lease.platform`, the `SessionSpec` of `lease.session_key`; `None` without a
provider) and `close()` closes them all (errors are logged with the module logger, never raised). `provider.open` runs
outside the `DriverPorts` lock (one open per lease id at a time; other leases and `close()` are never held up by it; a
failed open is forgotten and the next `port()` retries); after `close()`, `port()` raises `DriverError("driver_error")`,
and a port whose open finishes after `close()` is closed at once and the opener gets that error. `DriverRegistry` is a
hub-wide registry the run manager owns; a run's fake provider is never registered in it but passed only to that run's
`DriverPorts` (15.3).

## 5. Scheduler (P10, `engine/scheduler.py`)

### 5.1 Construction and the run loop

`Scheduler(run, plan, registry, executors, sessions, deps, config)`; `run()` blocks until the run is terminal and
returns `RunOutcome(status, reason, outputs, counts, cache)`. It journals through `deps.journal` (`RunJournal`), owns a
`ThreadPoolExecutor(max_workers=config.parallelism)` for node bodies, and loops: serve the debugger while paused (13.4);
start ready instances in ascending `seq` while fewer than `parallelism` are running; wait on its condition for a
completion, a control ticket or `WAIT_SLICE_SECONDS`; apply completions; activate and complete scopes; stop when the root
scope is final or the run failed or aborted. With `parallelism == 1` the journal order is the program order.

### 5.2 Readiness

A `pending` instance of node `N` at instance `I` is ready when:

* for every id `D` in `N.after`, the instance of `D` visible from `I` (the first `(D, P)` that exists for `P` in
  `instance_prefixes(I)`) exists and is settled (`SETTLED_STATUSES`); an instance that does not exist yet (a node inside
  a container body that has not activated, such as a `$ref` into a macro body) is not settled. When nothing runs,
  nothing is ready, nothing is paused and the root scope is not final, the run ends `error` with `engine: deadlock at
  <node> waiting for <ids>` (the validator's visibility and `after` rules make this unreachable for valid plans);
* in a plan of kind `seed`, `N` is `data.create`/`data.base` (`DATA_CREATE_OPS`) and every `data.extend` instance is
  settled, or `N` is not such a node (contracts-m0 10.3: extends run first, no edge encodes it);
* no other running leaf instance uses the same `lease.*` resource (one device action at a time; containers take the
  lease only during `enter` and `exit`, not while their body runs);
* the debugger is not paused (a requested pause or a step is taken by the next instance that starts: its `before_node`
  in 5.3 step 3 returns the decision, so the scheduler keeps starting instances until one pauses).

`after` edges carry program order per lane, `$ref` edges and clock provider edges (contracts-m0 5.4); the scheduler
relies on them and computes no lanes of its own. Nodes on independent lanes of a `ctl.parallel` body, and graph-ordered
pure nodes, run concurrently up to `parallelism`.

### 5.3 Starting a leaf instance

1. Mark `ready` (journal `node.ready`, `queued_at`), then `running` with `attempt += 1` (journal `node.started`).
2. Resolve arguments: `values.resolve_args(node, spec, store, I)`; `RefResolutionError` (`ref_unresolved`) or
   `NodeFailure("args_invalid")` fail the instance.
3. Debugger: `debugger.before_node(probe, store.snapshot())`; a decision pauses at the instance (status `paused`,
   13.3). Edited args (13.5) replace the resolved args.
4. `media.source` content check: for every `media.source` instance, `deps.content.media(uri)` runs first (a
   `ContentError` fails the instance: `content_mismatch` for a pinned digest that differs, BL-002 b) and the journal
   records `resolution.content = {uri, sha256, size, source}` (BL-002 c).
5. Pure ops go through the cache (section 9.7) and may finish here with a hit.
6. Ops located at `node`, `blender` or `browser_scene` acquire a render slot (`deps.slots.acquire`, 10.5) around the
   execution.
7. Build the `ExecContext` (4.2): the governing session is the innermost enclosing `score.take`'s session, else the
   plan session; the driver port is `deps.drivers.port(lease)` for nodes that use a lease (`None` otherwise); the
   deadline is 5.8.
8. Run the executor on the pool; the result or failure comes back to the loop.
9. Success: `values.checked_output(node, value)` (BL-006; an `OutputTypeError` fails the instance and the value is
   neither journaled nor cached nor stored), store the value (`store.put(N, I, value)`), journal the record (status
   `passed`, or `soft_failed` when `ExecResult.soft` is not empty; artifacts = the sink's and the result's;
   `executor` = the identity; `simulated`; `resolution.backend` = the `BackendIdentity` dump of the backend the node
   uses, if any), store into the cache (9.7).

### 5.4 Containers and expansions

Container instances (`spec.container`) start like leaves (steps 1 to 3), then choose branches:

| Op | Branches | Output |
| --- | --- | --- |
| `ctl.when` | `then` when `values.truthy(cond)`, else `else` when present, else none | `None` |
| `ctl.each` | `body` once per element of `over` (a JSON list, else `each_not_list`), `limit` elements at most | `None` |
| `ctl.parallel` | `body` | `None` |
| `ctl.macro` | `body` | the resolved `params` object |
| executor containers (`capture.record`, `net.cassette`, `score.take`, plugin containers) | `executor.container.enter(call).branches` | `exit(...)` result |

Activation instantiates every node of a chosen branch as `pending` at the branch instance: the container's instance,
or for `ctl.each` iteration `k` `child_instance(I, each_segment(N, k))` with `store.set_items(N, I, items)` first.
Each iterations run one after the other: iteration `k + 1` activates when every instance of iteration `k` is final. The
nodes of a `ctl.when` branch that is not taken are journaled as `skipped` (recursively) so the journal shows the whole
plan. Expansions are journaled with `RunJournal.expansion`: `branch` `{"branch": "then"|"else"|null}`, `each`
`{"count": n, "items": [...]}` (items are bounded with `journal.bounded_detail`).

A container instance stays `running` while its body runs. When every body instance is final: a hard-failed body instance
fails the container (`code` of the inner failure, message `body failed at <node>`); otherwise native containers pass
with the output above and executor containers call `exit` (its result goes through 5.3 step 9). An empty branch or an
empty each passes at once.

### 5.5 Dynamic nodes (`ctl.dynamic`)

1. Resolve `args.inputs` (5.3 step 2).
2. `deps.sandbox.dynamic(plan, node, instance=I, inputs=, tree=<run.tree>, plugins=<run.options.plugins>,
   digests=deps.digests (the tree registry's plugin digests by id), session=<the governing session>)` on a pool thread
   (section 12);
   failures fail the instance with the sandbox codes.
3. Validate the sub-plan: the validator with the run's tree registry; every node's `uses` names a resource of the
   parent plan with identical content; no node carries a `pin` when the dynamic node is inside a `score.take` (contracts-m0
   10.6); at most `SandboxLimits.max_nodes` nodes; a nested `ctl.dynamic` deeper than `config.max_depth` fails.
   Problems fail the instance with `subplan_invalid` and the issues in `detail`.
4. Journal the expansion `dynamic` `{"plan": <sub-plan JSON>, "fn": fn.hash, "inputs": value_digest(inputs)}` and
   activate the sub-plan's root nodes at `child_instance(I, dynamic_segment(N))`.
5. When every sub-plan instance is final: a hard failure fails the dynamic node; otherwise its output is
   `values.resolve(subplan.outputs, store, child)` (output type `json`).

Executors of sub-plan nodes come from `deps.choose` (the run's executor registry and fake source); an op without one
fails the instance with `executor_missing`.

### 5.6 Debug ops

`debug.break`: resolve `when` (absent means hit); `debugger.at_source(probe, label, hit, store.snapshot())`; the instance passes with
output `None` after a resume (or at once without a pause). `debug.pause_on_error`: `debugger.set_pause_on_error(session,
args.on)` for the governing session key; passes with `None`.

### 5.7 Failures, retries and soft failures

* A `NodeFailure` with `retryable=True` on an op with `OpSpec.idempotent` is retried after `RETRY_BACKOFF_SECONDS[n]`
  up to `config.retries` times (journal `node.retry`); nothing else retries automatically (design 5.2).
* `NodeFailure(soft=True)` ends the instance `soft_failed` without an output (`has_output=False`); a later `$ref` to it
  fails with `ref_unresolved` ("node X soft-failed and has no output").
* A hard failure asks `debugger.at_error(probe)`: a decision pauses at the instance (status `paused`, `PauseInfo.error`);
  otherwise the instance is `failed` and the run fails: the scheduler stops starting instances, sets `cancel`, waits up
  to `config.cancel_grace` for running instances (their results are journaled, late ones become `cancelled`), marks every
  remaining `pending`/`ready` instance `cancelled`, and returns `failed`.
* Before failing a node that used a lease, the scheduler takes failure shots: `port.snapshot(["screen"], scratch)`,
  ingested through the node's sink with the tag `failure`, journaled as `node.failure_shot` `{"artifacts": [...]}`;
  snapshot errors are journaled, never raised.

### 5.8 Timeouts

The deadline of an instance is `now + seconds` where seconds is the node args' `timeout` field when the op's model has
one and it is set (a `Duration`, ticks / `TICKS_PER_SECOND`), else the governing session's `timeout`, else
`run.options.node_timeout`, else `config.node_timeout`; `expect.value` with `eventually` adds its polling window. An
executor still running `TIMEOUT_GRACE_SECONDS` after its deadline is abandoned: the instance fails with `timeout` and
`cancel` is set when the run fails.

### 5.9 Outputs and the end of a run

When the root scope is final and nothing failed hard, `plan.outputs` is resolved at instance `""`
(`values.resolve`); a resolution error leaves `outputs` `None` and journals `run.outputs_unresolved` without failing the
run. The outcome carries counts and cache counts. `abort(reason)` (any thread) sets `cancel`, asks the debugger to stop,
and makes `run()` return `aborted` after the running instances end or `cancel_grace` passes.

### 5.10 Re-reads (`expect.value` with `eventually`)

`ExecContext.reread(expr)` (wired by the scheduler): `expr` must be a `$ref` whose producer instance (5.2 rule) is an
idempotent op; the scheduler re-executes that producer synchronously on the calling thread with the producer's own
executor (attempt + 1, journal `node.reread`), checks and stores the new output (later consumers see it), and returns
`values.resolve(expr, store, consumer instance)`.

### 5.11 Ad-hoc ops

The debugger's `do` action (13.5) runs `{op, args}` on the paused run: the op must exist in the run's registry, its
`resources` roles are filled from the paused node's session (or the plan session), the args are literal JSON validated
with `values.validate_args` (no refs), the executor comes from `deps.choose`, and the instance is journaled under node id
`adhoc:<n>` (n counts per run) at instance `""` with status `passed`/`failed`. Its value is the control response's
`result`.

## 6. Values (P1, `engine/values.py`)

* `ValueStore` is thread safe. `put(node, instance, value)`; `has`; `producer(node, instance)` is the first prefix of
  `instance_prefixes(instance)` holding a value of `node`, or `None`; `get` uses `producer` and raises `KeyError` naming
  the node and instance; `drop`; `set_items(each, instance, items)` stores an each node's items at its own instance;
  `items(each, instance)`; `item(each, consumer instance)` finds the segment `each#k` in the consumer's instance and
  returns `items[k]` of the each node's instance (the prefix before that segment); `snapshot(limit_bytes)` returns
  `{node_id or node_id@instance: value}` in insertion order, stopping (and adding `"...": "<n> more"`) once the compact
  JSON size passes the limit.
* `resolve(value, store, instance)` returns a copy of a JSON value with every `$ref` replaced by
  `apply_path(store.get(ref, instance), path)`, every `$item` by `apply_path(store.item(each, instance), path)`, and every
  `$fmt` by the concatenation of its parts where string values are inserted verbatim and other values as `fmt_text`
  (compact JSON: `json.dumps(v, separators=(",", ":"), ensure_ascii=False)`, decisions-m0 row "`$fmt` literal parts").
  Refs inside `find`/`filter` steps are resolved before the step applies. Any `KeyError`, `RefError` or step mismatch
  raises `RefResolutionError` naming the node, the expression and the cause.
* `resolve_args(node, spec, store, instance)` resolves `node.args` and validates them with
  `validate_args(spec, data)`: `model_validate(data)` of the resolved form of `spec.args` (a cached subclass of it, or
  `spec.args` itself when it has no ref positions, where every `$ref`/`$item`/`$fmt` member of a ref-typed field,
  nested models and `Poly` catalog fields included, is replaced by the runtime type of its ref kind: `str`, `int`,
  `float`, `bool` strict, `media` `MediaInfo`, `artifact` `Artifact`, anything else and ref-only conditions (`RCond`)
  `JsonValue`; ref-only fields such as `ctl.when.cond`, `ctl.each.over` and every `RMedia` reject resolved values in
  `spec.args` itself), `NodeFailure("args_invalid")` with the first three
  pydantic errors on failure, returning `ResolvedArgs(model, data)` where `data` is the validated model's dump
  (`mode="json", by_alias=True, exclude_none=True`).
* `truthy(value)`: `None`, `False`, `0`, `0.0`, `""`, `[]` and `{}` are falsy; everything else is truthy.
* `output_problem(out, value)` (BL-006): `none` requires `None`; `json` accepts any JSON value; `str` a `str`; `int` an
  `int` that is not a `bool`; `float` an `int` or `float` that is not a `bool` and is finite; `bool` a `bool`;
  `artifact` an object that validates as `ir.types.Artifact`; `media` an object that validates as `MediaInfo`;
  `list[T]` a list whose every item passes for `T`; `resource:K` any JSON object. It returns a message naming the
  expected type, the produced type and (for lists) the first bad index, or `None`. `checked_output(node, value)` raises
  `OutputTypeError` with that message and `detail {out, produced}` and otherwise returns the value unchanged.
* `value_digest(value)` is `ir.hashing.digest(ir.hashing.canonical_json(value))`.

## 7. Journal (P2, `engine/journal.py`, `db/migrations/0007_engine.sql`)

### 7.1 Tables

The migration (already written, normative) adds `runs`, `run_nodes`, `node_events`, `run_expansions`, `run_resources`
and `cache_entries` (section 9.5). JSON columns hold compact JSON (`db.common.dumps`); booleans are 0/1; times are epoch
seconds.

| Table | Row |
| --- | --- |
| `runs` | one per run: plan hash, kind, name, the full admitted plan JSON, spec, tree, project id, session id and name, user id, actor, instance, status, reason, options, the request without its plan (`request`), environment (`EnvironmentOut`), outputs, counts, cache counts, simulated, replay and repeat links, created, started and finished times |
| `run_nodes` | one per node instance: parent container (or dynamic node) and its instance, `seq`, op, node hash, label, src, status, attempt, resolved args of the last attempt, output (`has_output` tells `null` from absent), error (`NodeError`), cache outcome and key, resolution (machine resolution, content digests, backend identity), executor identity, location, simulated, edited, artifact ids, queued, started, finished, duration |
| `node_events` | the timeline: `id` (also the server-sent event id), run, node (null for run events), instance, attempt, time, type, detail |
| `run_expansions` | journaled sub-plans and branches: `each` items, `dynamic` sub-plans, `branch` choices |
| `run_resources` | every lease, backend, seed restore, cassette and slot the run held, with redacted identity (BL-005) |

### 7.2 API

`Journal(db)` methods (skeleton) map one to one onto the tables: `create_run` inserts the run (status `queued`) and
returns a `RunRow`; `run`, `runs(query, project_ids=, session_ids=)` (newest first; `project_ids`/`session_ids` restrict
to what a non-admin may see; the cursor is the base64 of `created_at` and id of the last row); `set_status`,
`set_environment`, `set_outputs`, `set_counts`; `upsert_node`, `node`, `nodes` (by `seq`); `append(run_id, event) ->
NodeEventRecord` (detail bounded by `bounded_detail`), `events(run_id, after=, node_id=, limit=)` (ascending id),
`event_count`; `record_expansion`, `expansions`; `set_resource`, `resources`; `unfinished()` (ids of runs whose status is
not terminal); `prune(before)` deletes terminal runs finished before `before` (cascades) and returns how many.
`bounded_detail` keeps a detail object under 16 KiB of compact JSON by truncating long strings and long lists and adding
`"truncated": true`.

`RunJournal(journal, publish, info)` binds a run: `event(...)` appends and then calls `publish.journaled(info, record)`;
`node(record)` upserts and journals nothing else; `status(status, reason=, pause=)` sets the status (and `started_at`
the first time it becomes `running`, `finished_at` when terminal), appends `run.<status>` with `{reason, pause}` and
calls `publish.activity` for `run.started` (first `running`), `run.paused`, `run.resumed` (`paused` -> `running`) and
`run.finished` (terminal); `expansion(...)` and `resource(...)` write and journal `node.expanded` /
`resource.<state>`; `progress(node_id, instance, attempt, fraction, message)` journals nothing and calls
`publish.progress(info, {"node", "instance", "attempt", "fraction", "message"})`. Every `RunJournal` method is thread safe.

### 7.3 Event types

Run events (node null): `run.queued`, `run.preparing`, `run.running`, `run.paused`, `run.passed`, `run.failed`,
`run.aborted`, `run.error`, `run.outputs_unresolved`, `run.warning` (admission warnings), `resource.<state>`,
`break.added`, `break.removed`, `break.error`, `control` (an action was received: `{action, actor}`). Node events:
`node.ready`, `node.started`, `node.log`, `node.artifact`, `node.cache` (`{outcome, key}`), `node.resolution`,
`node.retry`, `node.reread`, `node.expanded`, `node.args_edited`, `node.failure_shot`, `node.paused`, `node.passed`,
`node.soft_failed`, `node.failed`, `node.skipped`, `node.cancelled`. `node.progress` is published live only.

## 8. Events (P2, `engine/events.py`)

* Every journal append is published on the daemon `EventBus` as `run.event` with `persist=False`,
  `resource=run_resource(run_id)` (`"run:<id>"`), the run's `project_id` and `session_id`, and `detail` = the
  `NodeEventRecord` dumped by alias. `record_of(event)` turns such a bus event back into a record (others give `None`).
* Activity (`ACTIVITY_TYPES`: `run.created`, `run.started`, `run.paused`, `run.resumed`, `run.finished`) is published
  with `persist=True` into the daemon `events` table (the activity feed, retention `events.retentionDays`), detail
  `{run, plan: {kind, name}, status, reason}`.
* Progress (`RunEvents.progress(info, detail)`) is published as `RUN_PROGRESS` (`run.progress`) with `persist=False`,
  the run resource and the detail; it never enters the journal or the activity feed.
* `RunEvents.stream(journal, run_id, last_event_id=, is_disconnected=, heartbeat=, finished=)` returns an async iterator
  of server-sent event bytes: `retry: 3000`, then the journal events after `last_event_id` (pages of `REPLAY_PAGE`, read
  with `asyncio.to_thread`), then live `run.event`s of that run from a bus subscription opened before the replay
  (duplicates by id are dropped), live `run.progress` events as `event: progress\ndata: <detail JSON>\n\n` frames without
  an `id:` line (never replayed), a `: keep-alive` comment every `heartbeat` seconds, and once `finished()` is true and
  the journal has nothing newer, a final `event: end` frame whose data is the run status; it returns when the client
  disconnects or the subscription overflows. `format_run_sse(record)` is `id: <id>\nevent: <type
  prefix: run|node|resource|break|control>\ndata: <record JSON by alias>\n\n`.

## 9. Cache (P4, `engine/cache.py`)

### 9.1 What is cached

Only instances of pure ops (`OpSpec.pure`) whose `ir.hashing.cache_inputs(plan, node, registry)` is not `None`, in cache
modes `use` (read and write), `refresh` (write only), `readonly` (read only); `off` neither. Sub-plan nodes of dynamic
functions are cached like any other pure node (their `plan` is the sub-plan in every call below); instances whose args
the debugger edited bypass the cache.

### 9.2 Key (BL-001, BL-002, BL-003, BL-004, BL-008, BL-009, BL-010)

`cache_key(CacheKeyRequest) -> CacheKey | Bypass`. The material is:

```json
{
  "v": "eks-cache@1",
  "node": "<node.hash>",
  "op": "<node.op>",
  "executor": {"name": "...", "version": "...", "runtime": "<runtime_version() or null>",
               "harness": "<harness_digest() for eks provenance, else null>",
               "fonts": "<fonts_digest() when executor.fonts, else null>"},
  "inputs": {"<producer id>": "<content digest>"},
  "resolution": {"...": "..."},
  "plugins": {"<plugin id>": "<plugin digest>", "project": "<tree code digest>"},
  "clock": {"tempo": "...", "cues": "...", "markers": "..."}
}
```

and the key is `digest(canonical_json(material))`. Rules:

* `inputs` holds every id of `cache_inputs(plan, node, registry)` other than the node itself and every node of
  `ir.hashing.pure_closure(plan, node, registry)` other than the node itself (the pure producers upstream), in program
  order, each with its content digest from `gather_inputs` (9.3), and, when the node itself is content-dependent
  (`ir.hashing.content_dependent`: a `media.source` read by content), the node's own id with
  `content.media(uri).sha256`. Keying the pure producers by what they produced carries every upstream executor version,
  `runtime_version()`, harness and fonts digest and plugin or project executor code into the reader's key (a pure
  producer runs or is served before its readers, so its value is there). So a node is never served by its node hash
  alone unless it reads no producer.
* `resolution` is present (else `null`) exactly when `ir.hashing.machine_dependent(node, registry)` is true: it is the
  whole record the resolver reported (4.6), keys sorted, which must contain at least the `required_resolution(node,
  registry)` fields. A machine-dependent node whose executor has no resolver is `Bypass("<op> is machine-dependent and
  its executor has no resolver")`: it is executed and never stored or served (BL-001).
* `plugins` (BL-004): for the node and every node of `ir.hashing.pure_closure(plan, node, registry)`, every
  `ir.contributions.node_contributions(node, registry, scope=<governing session scope>)` entry and the op's own
  provenance name a plugin; each plugin other than `eks` and `project` contributes `digests.plugin(id)`, and any
  `project` contribution or project op adds `"project": digests.project()`; the executor's own
  `ExecutorSpec.provenance.plugin` is added the same way (a plugin or project executor serving a built-in op is keyed
  by its code). A `None` digest is `Bypass("plugin <id> is not loaded on this hub")` / `Bypass("project code without
  a tree")`.
* `clock` (BL-010) is `clock_material(plan, request.clock)`: the run clock dumped canonically when the plan's tempo
  is external, else `null`.
* `harness_digest()` is `_version.build_info()["sourceHash"]` computed once per process; `fonts_digest()` is
  `plugins.trust.content_hash(Path(eks_harness.annotate.__file__).parent / "fonts")` computed once per process (BL-003).

### 9.3 Content digests and content resolution

`gather_inputs(plan, node, registry, value_of=, content=, artifacts=, override=, item_of=)` returns `{producer id:
digest}` for the ids of 9.2 where `value_of(id)` is the producer's value at the consumer's instance (the scheduler
passes `lambda id: store.get(id, instance)`), `item_of(each)` the current item of an each node at the consumer's
instance (the scheduler passes `lambda each: store.item(each, instance)`; a `$item` read without it is a `ValueError`)
and `override(id)` the `ExecResult.content` the producer instance recorded, if any:

* a `media.source` producer: `content.media(uri).sha256` (from its args, not its output; BL-002 a); a source of kind
  `bundle` with a `file:`, `plugin:` or `pkg:` URI is digested by the directory around its entry
  (`plugins.trust.content_hash(entry.parent)`, memoized per resolver by the size and mtime of every file in it);
* a `ctl.each` producer: `value_digest(item_of(id))` when the node or its pure closure reads it only through `$item`,
  `value_digest(value_of(id))` when only through `$ref`, and `value_digest({"item": <item digest>, "value": <value
  digest>})` when through both;
* otherwise `content_digest(value, producer.out, artifacts, content=content)` (the keyword resolver hashes media URIs;
  without it only plain `artifact:ID` URIs resolve and others raise `content_unavailable`): `artifact` -> the store's current sha256 of the artifact
  (`content_unavailable` when the artifact is gone); `media` -> the artifact's sha256 when `artifact` is set, else
  `content.media(uri).sha256` for `artifact:`/`file:`/`plugin:`/`pkg:`/`http(s):` URIs, else `value_digest`;
  `list[...]` of those -> `value_digest` of the element digests; anything else `value_digest(value)`. A non-`None`
  `override(id)` wins over all of these.

`ContentResolver.media(uri) -> MediaContent` (memoized per resolver, thread safe):

* `artifact:ID` -> the store's sha256 of ID (hashing the file when the store row has none), size, `source="artifact"`;
* `artifact:ID@sha256:HEX` -> the same, and `ContentError("content_mismatch", ...)` when HEX differs ("artifact ID now
  holds sha256:<current>; the plan pins sha256:HEX. Restore that version or plan again with the new digest", BL-002 b);
* `file:PATH` -> PATH relative to `tree / base` (absolute paths as written), sha256 of the bytes, memoized by
  `(path, size, mtime_ns)`; a missing file is `content_unavailable`;
* `plugin:ID/PATH` -> `plugin_roots[ID] / PATH`; `pkg:MODULE/PATH` -> the directory of
  `importlib.util.find_spec(MODULE).origin` / PATH;
* `http(s)://...` -> streamed into `scratch` with a 60 s timeout (per operation and for the whole download) and
  `HTTP_LIMIT_BYTES` limit and hashed; `ContentResolver.cancel` (`threading.Event | None`, the constructor keyword
  `cancel=`, default `None`) is the run's cancel event: the scheduler sets `deps.content.cancel = self.cancel` when it
  is constructed, and setting the event ends a running download at once with `NodeFailure("cancelled")`, releasing
  the URI for other threads;
* digests are `"sha256:<hex>"` (the artifact store keeps plain hex in `artifacts.sha256`; `ir.types.Artifact.sha256`
  and every engine digest carry the `sha256:` prefix, and comparisons strip it); failures raise
  `ContentError("content_unavailable", ...)` naming the URI and cause. Artifact files are
  `store.layout.file_path(paths, row.rel_path)`.

### 9.4 Resolution requirements

`required_resolution(node, registry)` returns dotted field paths that must be present and non-empty in the record:

| Node | Fields |
| --- | --- |
| `media.markers` with an unpinned `BeatTracker`-like source `S` | `markers.S.backend`, `markers.S.version` |
| `media.markers` with an unpinned `STTMarkers` source `S` | `markers.S.model`, `markers.S.device`, `markers.S.compute_type`, `markers.S.backend`, `markers.S.version` |
| machine-dependent `check.media` | `engine`, `version` |
| `edit.render` with a non-software encoder | `encoder`, `encoder_version` |
| `edit.render` with a device-dependent effect (BL-008) | `inference.device`, `inference.model`, `inference.weights`, `inference.backend_version` |
| `gfx.render` without `cpu` | `gpu.adapter`, `gpu.driver` |
| `blender.render` CPU Cycles without a platform pin | `platform` |
| `blender.render` on GPU, EEVEE or Workbench | `gpu.adapter`, `gpu.driver` |
| `scene.render` | `browser` |
| an op with `OpSpec.device_dependent` (`media.tts`, `media.stems`, plugin ops) | `device`, `model`, `weights`, `backend_version` |
| `audio.mix` with `stems` or `denoise` | `inference.device`, `inference.model`, `inference.weights`, `inference.backend_version` |

`resolution_problems(node, registry, record)` lists the missing fields; the scheduler fails the node with
`resolution_incomplete` when the predicted record has problems, and with `resolution_mismatch` when `ExecResult.resolution`
differs from the prediction on a required field. Blender, Cycles and OIDN versions of every `blender.render` reach the
key through the executor's `runtime_version` (BL-009); "all frames on one platform" and "a pinned render without a
worker of that platform fails" are the real executor's duties (M5) and its resolver raises `resolution_unavailable`
then.

### 9.5 Storage

* Index: `cache_entries` (key, node hash, op, value JSON, blobs JSON `[{sha256, size, name, kind, artifact}]`,
  material, resolution, size, producing run, created, last used, hits).
* Blobs: a content-addressed store under `<paths.cache_dir>/engine-cas/<hex[:2]>/<hex>` (`CacheService.root` is
  `paths.cache_dir`), written by hard link from the artifact's file when possible, else by copy to a temporary name and
  rename, and verified against the artifact's sha256 before the index row is written. `blob_path(sha256)` is that
  path.
* `size` is the value JSON's byte length plus the blob sizes.

### 9.6 Lookup, store and eviction

* `lookup(key, artifacts=, materialize=)`: no entry -> `None`. For each blob: the artifact still exists with the same
  sha256 -> kept; else the CAS file exists -> `materialize(blob, path)` ingests a copy into the current run's session and
  the mapping old id -> new `Artifact` is recorded; else the entry is deleted and `None` returned. The value is
  `relink(entry.value, mapping)` (an object with `"id": old` and the `Artifact` keys becomes the new artifact dump, a
  string `old` becomes the new id, `artifact:old` the new URI, a `MediaInfo` `artifact` field and `uri` follow); hits and
  `last_used_at` are updated; the result is `CacheHit(entry, value, artifacts, rematerialized)`.
* `store(key, node=, value=, blobs=, resolution=, run_id=, artifacts=)`: `artifacts[i]` is the id of the produced
  artifact `blobs[i]` copies (`BlobRef` carries no id and lookup needs it; a length mismatch is a `ValueError`); writes blobs and the row (replacing an older row of the
  same key; when the old value differs, the scheduler journals `node.cache` `{outcome: "stored", diverged: true}`).
* `evict(now=)`: deletes entries unused for `max_age_days`, then least recently used entries while the total size is
  above `max_bytes`, then CAS files no entry references; returns `EvictionReport`. `stats()` sums the index and the CAS.
* Counting (`CacheCounts`): `hits` (served), `misses` (looked up, executed), `stored` (written), `bypassed` (`Bypass` or
  edited); `off` mode counts nothing.

### 9.7 Scheduler flow for a pure instance

1. Content check of `media.source` (5.3 step 4).
2. `resolution = executor.resolver(ResolveContext(...))` when the node is machine-dependent; resolver failures fail the node
   (`resolution_unavailable`), `resolution_problems` fail it (`resolution_incomplete`); the record is journaled
   (`node.resolution`) and stored on the instance.
3. `key = cache_key(...)`; `Bypass` -> execute, outcome `bypass`.
4. Modes `use`/`readonly`: `lookup`; a hit finishes the instance (`passed`, outcome `hit`, the hit's artifacts, the value
   through `checked_output`).
5. Execute; on success with mode `use`/`refresh` and no soft failure, `store` with the blobs of the produced artifacts
   (`ArtifactStore.file` and the artifact's sha256); outcome `stored`, else `miss`.

### 9.8 Invalidation

Keys change whenever anything that changes output changes, so entries are never invalidated in place; superseded
entries age out through `evict`. Executor and harness changes change `executor`; plugin code changes change `plugins`
(BL-004); content changes change `inputs` (BL-002); machine changes change `resolution` (BL-001, BL-008, BL-009); a run
clock changes `clock` (BL-010). Hub registries are invalidated separately (16.2).

### 9.9 IR additions (P4)

* `ir.registry.CatalogSpec.device_dependent: bool = False`; `catalog(..., device_dependent=False)` and
  `effect(..., device_dependent=False)` (both overloads) pass it; a `True` value on any category other than `effect`
  raises `RegistryError`. `eks.effect` (the re-export) gains the keyword.
* `ir.catalog.effects`: `MatAnyoneRemove`, `BiRefNetRemove`, `SAM3Track`, `AutoReframe`, `PunchIn` are registered with
  `device_dependent=True`.
* `ir.hashing.machine_dependent(edit.render)` is also true when any effect of any clip of the timeline (validated through
  the registry; plugin effects included) has `CatalogSpec.device_dependent` (BL-008 1 and 2); an `edit.render` whose args
  do not validate stays machine-dependent, except when every validation error is an effect kind the evaluating registry
  does not hold (M0 keeps `cacheable` true for a plugin effect evaluated outside its registry,
  `tests/eks/test_hash_contributions.py`); `cache_key` never keys such a node, since `plugin_material` cannot validate
  its args and returns `Bypass`.
* `ir.hashing.pure_closure(plan, node, registry) -> tuple[str, ...]`: the node and every transitive pure producer of its
  data inputs (the same walk as `cache_inputs`: `$ref`/`$item` producers and clock providers, through pure nodes only),
  in program order.
* `ir.hashing.node_needs(node, registry) -> TimeNeeds | None`: the public form of `_node_needs` (the needs of the node's
  validated args and pin, `None` when the op is unknown or the args do not validate).

## 10. Resources (P6, `engine/resources.py`, `renderq.py`)

### 10.1 Planning

`ResourcePlanner.plan(plan, registry=, tree=, sessions=, backend=, backends=) -> ResourcePlan` (no side effects):
`leases` = one `LeaseNeed` per `lease.*` resource (`platform` = the kind suffix, `session_key` = the resource's session or
`main`, `backend_resource` = the backend resource of the same session); `backends` = one `BackendNeed` per `backend`
resource (`ref` = `backends[session_key]`, else `backend`, else `None`; `source` = `"request"` with a ref, `"plan"` with a
definition, else `None` until chosen; `seed` and `seed_hash` = `eks.workspace.seed_info(seed, registry=registry).hash`
when the seed is known); `cassettes` = one `CassetteNeed` per `net.cassette` node; `slots` = the distinct
`node`/`blender`/`browser_scene` locations of the plan's ops.

### 10.2 Backend choice and identity (BL-005)

`choose_backend(need, registry=, tree=, session=) -> BackendChoiceOut`, first match wins:

1. `need.ref` -> source `request`; the definition is the record's definition for a `bl_` lease id or a backend id
   (`definition@identity`), else the ref itself.
2. `resource.definition` -> `plan`.
3. The session app's profile `[backend]` table (`eks.discovery.find_app(tree, app).tables["backend"]`): `link` or
   `definition`, with `tree`, `key`, `params` -> `profile`.
4. `registry.backend_policies()` in name order: the impl (`module:qualname`) is instantiated with no arguments and
   `choose(tree, None)` called; the first `BackendChoice` -> `policy` with the policy name and its provenance.
5. `backends.choose_backend(ctx.plugins, tree)` (legacy `[contributes] backend_policy`) with a decided backend ->
   `legacy_policy` with the contribution key and its plugin's version.
6. The project's `[backends] default` -> `project_default`.

Otherwise `ResourceError("no backend for session <key>: name one with backend= on the flow or its seed, pass --backend,
or add a backend policy")`. The identity of an acquired backend is `BackendIdentity(definition, backend_id, lease_id,
source, policy, policy_plugin, policy_version, plugin, plugin_version, fingerprint, commit, branch, version,
generation)`: `plugin`/`plugin_version` from `registry.backend(definition).provenance` when the definition is an
`@eks.backend`, else from the legacy plugin definition's plugin and its manifest version, else `None`; fingerprint from
the backend record, commit, branch and version from its info, generation from the record. Acquisition fails with
`ResourceError` when `plan.requires` names the backend's plugin at another version than the identity's, and when a
policy, profile, request or project default chose a definition whose plugin is not active in the tree registry (BL-005
2). The identity is journaled in `run_resources.detail`, in `run_nodes.resolution.backend` of every node that uses the
backend, and in `EnvironmentOut.backends`; it enters the seed snapshot key and the cassette key (10.3, 10.4; BL-005 3).

### 10.3 Acquisition

`acquire(run, plan, needs, registry=, sessions=, sink=, cancel=, seed_mode=, seed_hook=, cassette_hook=) ->
HeldResources`, in this order, every step
recorded through `sink.record(ResourceRecord)` (`pending` -> `acquiring` -> `held` or `failed`) and `sink.event`:

1. Backends: choose; acquire a run-owned backend lease with `ctx.pools.backends.acquire({"definition", "tree": ref tree or
   run tree, "key", "params", "id": record id for bl_/backend-id refs, "kind": "agent", "holder": f"run {run.id}",
   "owner": f"run:{run.id}", "project", "session", "ttl": BACKEND_TTL, "userId"})` (a caller's `bl_` lease is never taken
   over; the run holds its own lease on the same instance); `wait_ready(backend_id, backend.readyTimeoutSeconds,
   abort=cancel.is_set)`; `failed`/timeout -> `ResourceError`; the handle gets `url` = info url, redacted `info`
   (`backends.info(id)`) and `secrets` (`backends.info(id, secrets=True)`, excluded from every dump).
2. Seeds (`seed_mode == "restore"` and the backend has a seed): `seed_hook` (the per-run hook, a fake profile's
   `FakeSeedHook`) or else `seeds.hook(definition)` restores with `SeedRestoreRequest(... key=seed_key(seed_hash,
   identity))` and the backend's revealed info; no hook -> `ResourceError` (`seed.hook` text of 3.7). Journaled as kind
   `seed`, resource id `seed:<backend resource id>`, handle = the key.
3. Leases: `daemon.leases.manager(ctx).acquire(body, actor, wait=True, wait_seconds=LEASE_WAIT_SLICE)` with body
   `{"kind": browser|ios|android, "project", "session", "instance": f"run:{run.id}" (f"run:{run.id}/<resource id>" for
   every further lease of the same kind, since the lease manager hands a live lease of the same instance and kind back),
   "owner_pid": os.getpid(),
   "owner_started": pools.backends.pid_started(os.getpid()), "tree", "label": f"run {run.id}", "tags": [f"run:{run.id}"],
   "claim": {"run": run.id, "pid": os.getpid(), "started": ..., "host": socket.gethostname()}, "backend": <held bl_ lease id
   of the session> or absent}`, then `wait_for(lease id, True, LEASE_WAIT_SLICE)` until `granted` (-> `LeaseHandle`),
   `failed` (-> `ResourceError` with the lease error) or `cancel` (release, `ResourceError("cancelled")`).
4. Cassettes in `replay` mode: `cassette_available` false -> `ResourceError` with `cassette_missing`.

Any failure releases what was acquired (10.6) and raises. `HeldResources.resources` is the `RunResources` the scheduler
sees.

### 10.4 Hooks

* Seed hook (`SeedHook.restore(request, info) -> SeedRestoreResult`); `SeedHooks.register(definition, hook)` (`"*"`
  matches every definition; an exact definition wins); M3 registers the real seed engine. `acquire(seed_hook=)` and
  `cassette_available(hook=)` take a per-run hook that wins over the registered ones (fake profiles use it).
  `seed_key(seed_hash, backend) = digest(canonical_json({"seed": seed_hash, "backend": {definition, plugin,
  plugin_version, fingerprint, version, commit}}))` (design 7 adds the migrations hash through the fingerprint in M3).
* Cassette hook (`CassetteHook.available/opened/closed`); `CassetteHooks.register(hook)`, the last one wins.
  `cassette_key(plan, node, backend) = digest(canonical_json({"plan": plan.hash, "node": node.hash, "name":
  node.args["name"], "backend": {definition, fingerprint, version, commit} or null}))` (design 8: keyed by plan and
  backend fingerprint). The `net.cassette` container executor (fake in M1, real in M2/M4) calls `opened` on enter and
  `closed` on exit; `closed` returns the cassette artifact id.

### 10.5 Render slots

`SlotManager.acquire(location, label, run_id=, cancel=, on_wait=None)` wraps `renderq.claim(kind=location, label=label,
session=run_id, cancel=cancel.is_set)` (calling `on_wait(position, ticket id)` whenever the queue position changes, so
the scheduler can journal the wait below) and returns `SlotHandle(ticket, location, label, acquired_at)`; `release`
calls `renderq.drop(ticket)`. `renderq` gains (P6): `claim(kind, label, *, session=None, cancel=None, on_wait=None) ->
Ticket` (writes the ticket and blocks until `len(running) + position < concurrency()`, the same FIFO as `render_slot`,
without touching `os.environ` or thread locals; raises `RuntimeError("cancelled")` and removes its ticket when
`cancel()` is true) and `drop(ticket: Ticket | str)`; `render_slot` keeps its behaviour and is rewritten on top of them.
Slot waits are journaled (`resource.acquiring` with the queue position) as resource kind `slot`, id `slot:<ticket>`.

### 10.6 Keepalive and release

`keepalive(held)` heartbeats every driver lease (`LeaseManager.heartbeat(sid)`), renews every backend lease
(`backends.renew(lease_id, BACKEND_TTL)`) and sets `lease_manager.last_activity`; a lease or backend lease that is gone
journals `resource.lost` (the next node that uses it fails with `resource_error`). The run manager calls it every
`KEEPALIVE_SECONDS` while a run holds resources (paused runs included). `release(held, reason, sink)` releases driver
leases (`LeaseManager.release(sid, reason, actor=f"run:{run.id}")`), then backend leases (`backends.release(lease_id,
reason)`), marking records `released`; errors are journaled, never raised.

## 11. Persona auth (P6, `engine/auth.py`)

`PersonaAuthRegistry.headers(AuthRequest) -> Mapping[str, str]` resolves, first non-`None` wins:

1. providers registered with `register(provider, definition=)` for the backend's definition, then those for `"*"`, in
   registration order;
2. `BackendHookAuth`: when `request.registry.backend(definition)` exists, its impl class attribute `persona_headers` (a
   static or class method `(persona: str, info: plugins.sdk.BackendInfo) -> Mapping[str, str] | None`) is called with
   `BackendInfo.from_dict(backend.secrets)`;
3. `CredentialsAuth` over the revealed credentials (`backend.secrets["credentials"]`): the entry
   `credentials["personas"][persona]`, else `credentials[persona]`, is a mapping with `headers` (used as given), or
   `token` (`Authorization: Bearer <token>`), or `username` and `password` (HTTP Basic), or `api_key` with optional
   `header` (default `X-Api-Key`).

Nothing found raises `PersonaAuthError("backend <definition> offers no credentials for persona <persona>; add them to
the backend info credentials or register a persona auth provider")`. Results are cached per `(run id, backend id,
generation, persona)` until `forget(run_id)`; `can_authenticate` answers without raising. Headers and credentials never
reach the journal (`plugins`-style `secret_strings` scrubbing applies to journaled request summaries, section 20).

## 12. Dynamic sandbox (P7, `engine/sandbox.py`, `engine/sandbox_child.py`)

### 12.1 Parent

`SandboxRunner.run(request) -> SandboxResponse` starts `[python or sys.executable, "-P", "-s", "-B", "-m",
CHILD_MODULE]` with `cwd` a fresh empty directory under `scratch`, environment `child_env(os.environ, home)`
(`PYTHONHASHSEED=0`, `ENV_KEEP` variables, every `EKS_HARNESS_*` variable so the child resolves the same paths and
config, `HOME` set to the fresh directory, nothing else), stdin the request JSON, stdout limited to
`OUTPUT_LIMIT_BYTES`, stderr kept up to `STDERR_LIMIT_BYTES`; it kills the child after `limits.timeout_seconds` (response
`ok=False`, `code="limit"`). A missing or unparsable response is `code="crash"` with the stderr tail. `-I` is not used
because it would drop `PYTHONHASHSEED` (contracts-m0 5.8 requires it).

`dynamic(plan, node, *, instance, inputs, tree, plugins, digests, session) -> SubPlan` builds the request (node, plan and
the governing session dumped by alias), runs it, and maps failures to `SandboxFailure`: `trace_error`/`user_error` -> `sandbox_error`,
`violation` -> `sandbox_violation`, `limit` -> `sandbox_timeout`, `registry_changed`/`protocol`/`crash` ->
`sandbox_error`, each with the child's message, line and traceback frames in `detail`. Success returns the validated
`Plan.from_json` of `response.plan` (a `Plan` that does not validate is `sandbox_error`).

### 12.2 Child

`main()` reads stdin, calls `run(read_request(data))`, writes the response JSON to stdout and returns 0 (1 for a request
that does not parse, with a `protocol` error). `run(request)`:

1. `build_registry(request)`: `eks.workspace.workspace_registry(tree, plugins=request.plugins, isolated=True)` (section
   16.2) and a digest check: every plugin in `request.digests` must be active with the same digest
   (`engine.registries.plugin_digest`), else `registry_changed`.
2. `guard(request.limits)` (default `install_guard`; in-process tests pass a guard that installs nothing, because an
   audit hook can never be removed from the process that added it): applies `apply_limits(limits)` (`resource.setrlimit` for `RLIMIT_CPU`,
   `RLIMIT_AS` (or `RLIMIT_DATA` where `RLIMIT_AS` cannot be set, macOS), `RLIMIT_FSIZE` 0 (with `SIGXFSZ` ignored),
   `RLIMIT_NOFILE`, `RLIMIT_CORE` 0; each limit that cannot be applied is reported, not fatal), tries
   `os.unshare(os.CLONE_NEWNET)` on Linux, and installs `audit_hook` with `sys.addaudithook`, which raises
   `PermissionError` for every event in `BLOCKED_EVENTS` and for `open` with a write, append or create mode. The result
   is `SandboxResponse.isolation` (`{"limits": {...}, "network": "audit" | "audit+netns", "files": "audit+fsize"}`).
3. `trace_dynamic(request, registry)`: `fn = FunctionSource` of the node args; `compile_function(fn.name, fn.source,
   node id)` compiles under the file name `SOURCE_PREFIX + node_id + ">"` and registers the lines in `linecache` so call
   texts hash; a `TraceContext(kind=<"flow" when request.session is set, else plan.kind>, name=sandbox.subplan_name(plan,
   node), registry=, clock=plan.clock, session=<request.session, the SessionSpec governing the node>, tree=None,
   entry_code=<the def's code object>)` declares every parent resource again (`ctx.resource(kind, **identity)`, ids must
   match), so a take's dynamic function validates as a flow on the take's session; `bindings(...)` rebuilds the globals:
   `__builtins__` holds only
   `SANDBOX_BUILTINS`, `eks`, `by`, `t`, `expect` are the real objects, and each `fn.freevars` binding is rebuilt
   (`session` -> `eks.session.Session(ctx, spec of that key)`, `namespace` `"<key>.<attr>"` -> that attribute of the
   session, `literal` -> the JSON value, `time` -> `parse_time`, `target` -> `Target.model_validate`, `factory` -> `by`,
   `t` or `expect`); the function is built as `types.FunctionType(<the def's code object>, <those globals>, fn.name)`
   (the `def` statement never runs: its plan-time defaults and annotations name refs and flow locals the sandbox does
   not have, and `request.inputs` binds every parameter, so inputs that do not match `fn.params` are a `TraceError`); inside `using_registry(registry)`, `tracing(ctx)` and the bound
   session's `activate()`
   (the only `session` binding, else the plan session) the function is called with `request.inputs` as keyword
   arguments; `ctx.set_outputs(result)`; `ctx.finalize()` returns the sub-plan (link_clock and hashes included).
   `subplan_name(plan, node)` is `f"{plan.name}~{node.id}"`, so the sub-plan's root scope key, node ids and hashes depend
   on the node, not on the iteration (the instance key keeps iterations apart).
4. Errors, first match wins: a `PermissionError` raised by the audit hook -> `violation`; `MemoryError`,
   `RecursionError` -> `limit`; the authoring errors `TraceError`, `RefUsageError`, `RegistryError`, `TargetError`,
   `TimeError` and pydantic `ValidationError` -> `trace_error`; any other exception whose innermost traceback frame
   belongs to the compiled function (`SOURCE_PREFIX` file name) -> `user_error` with that line; anything else ->
   `trace_error`. `traceback` lists the frames of the function source only. The response always reports `isolation`
   and `duration_ms`.

## 13. Debugger (P1, `engine/debugger.py`)

### 13.1 Breakpoints

```text
spec      ::= criterion ( "," criterion )*
criterion ::= "node:" NODE_ID | NODE_ID | "op:" GLOB | "label:" TEXT | "when:" EXPR
```

`when:` takes the rest of the string (commas included), so it comes last. A bare criterion must match
`ir.plan.NODE_ID_PATTERN`. `GLOB` is matched with `fnmatch.fnmatchcase` against the op name. Every criterion of a spec
must hold. `label:` matches the node's `label` or the `label` arg of a `debug.break`. Empty specs, unknown prefixes and
bad expressions raise `ValueError` naming the spec. `parse_breakpoint` returns `BreakpointSpec`; `Debugger.add` creates
`Breakpoint(id="b<n>", text, ...)` (n counts per run from 1). The debugger journals through its `journal(type, detail)`
constructor callback (the run manager passes the run journal's `event`; `None` journals nothing): `break.added`
`{"breakpoint": <Breakpoint dump>}` for `add` (constructor breakpoints included), `break.removed` `{"id"}` for a removal
that found the breakpoint, and `break.error` `{"id", "message"}` when a condition fails to evaluate. The callback is
called outside the debugger's lock.

### 13.2 Conditions

`Condition(text)` parses `ast.parse(text, mode="eval")` and accepts only: the names `CONDITION_NAMES` (`node`, `op`,
`label`, `src`, `attempt`, `instance`, `args`, `values`, `error`), `True`, `False`, `None`, string and number
constants, attribute access (a key lookup on objects), subscripts with constant keys or indices, comparisons (`==`,
`!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, chains), `and`, `or`, `not`, unary `-`, binary `+ - * / %`, list and tuple
displays of those, and calls of `CONDITION_FUNCTIONS` (`len`, `contains(a, b)`, `startswith`, `endswith`,
`matches(text, pattern)`, `lower`, `upper`) with positional arguments; the pattern of `matches` must be a string constant, is compiled when the condition is parsed and runs on a linear-time engine (no `re`): an unanchored search in Python regex syntax without backreferences, lookarounds, inline flags, conditional, atomic or possessive forms, at most 512 program steps and 32 nested groups, and at most 1000000 steps per search (beyond that it raises `ConditionError`); at most
`CONDITION_MAX_LENGTH` characters and `CONDITION_MAX_DEPTH` levels; anything else raises `ConditionError` naming the
construct. `evaluate(scope)` returns the truthiness (`values.truthy`) of the result; a missing key is `None`; type errors
raise `ConditionError`, which the debugger journals as `break.error` and treats as "no hit". `NodeProbe.scope(values)`
builds `{node, op, label, src, attempt, instance, args, values, error}`.

### 13.3 States

`DebuggerState`: `off` (constructed with `enabled=False`: breakpoints are kept and listed but never pause; `pause`,
`continue`, `step`, `retry`, `skip`, `edit`, `do` answer `ok=False` "debugging is off for this run; start it with
--debug"), `running`, `paused`, `stepping` (resumed with `step`: the next instance to start pauses with reason `step`),
`aborting`.

Pauses (`enter_pause(PauseInfo)`, from the scheduler thread): `before_node` returns a `PauseDecision` when a breakpoint
matches (its hits increase; reason `breakpoint`), when stepping (reason `step`) or when a pause was requested (reason
`pause`); `at_source` when a `debug.break` is hit (reason `source`); `at_error` when `break_on_error` or the session's
`pause_on_error` is on (reason `error`). Nothing pauses while `off`.

### 13.4 Tickets

`submit(request, timeout)` runs on the request thread: `status`, `break_list`, `break_add`, `break_remove` and `pause` are
answered at once (`IMMEDIATE_ACTIONS`); `abort` sets `abort_requested` and, when paused, also queues a ticket; the
`PAUSED_ACTIONS` (`continue`, `step`, `retry`, `skip`, `edit`, `do`) require the paused state (else `ok=False`) and
queue a `ControlTicket` that the scheduler answers; `submit` waits up to `timeout` for `ticket.done` and otherwise answers
`ok=False` "the run did not answer within <timeout> s" (withdrawing the ticket, so it is never applied; a ticket the
scheduler already took answers `ok=False` "the run took <action> but did not finish it ..."). The paused check and the
queueing are one atomic step, and a ticket belongs to the pause it was queued in: `enter_pause` and `leave_pause` answer
every ticket still queued `ok=False` ("the run resumed before <action> was applied"), and an abort answers the queued
non-abort tickets the same way. `next_ticket(cancel, timeout)` (scheduler thread, while paused) pops the next ticket and
returns `None` on `cancel`, `close` or a requested abort when nothing is queued; `answer(ticket, response)` completes
it; `leave_pause(mode)` resumes (`continue` or `step`);
`request_abort()` and `close(message)` (run ended: every queued or taken ticket is answered `ok=False` with the message, state
`off`) are thread safe. Every response carries the run status (`status()` callable), the debugger state, the pause and
the breakpoints.

### 13.5 Actions while paused (scheduler side)

| Action | Effect |
| --- | --- |
| `continue` | resume; a paused-before instance starts, a failed instance stays failed unless retried (the run then fails) |
| `step` | as continue, then pause before the next instance that starts |
| `retry` | run the paused instance again (attempt + 1), with edited args when edited |
| `skip` | the paused instance becomes `skipped` with output `request.value` (checked against `node.out` unless `null`) |
| `edit` | replace the paused instance's args with `request.args` (validated with `values.validate_args`); journal `node.args_edited`; stay paused; the instance is `edited` and never cached |
| `do` | run `{op, args}` as an ad-hoc instance (5.11); stay paused; `result` is its value |
| `abort` | the run ends `aborted` with reason `aborted from the debugger` |

A pause records `PauseInfo` (reason, node, instance, attempt, op, label, src, resolved args, error, breakpoint id,
`ValueStore.snapshot()`, snapshots, time); for nodes with a lease the scheduler adds `snapshots` from
`port.snapshot(["screen", "dom"], scratch)` ingested as artifacts tagged `pause`. While paused, running instances finish
and are journaled; a pause longer than `config.pause_timeout` aborts the run (reason `paused longer than <n> s`).

## 14. Run report (P2, `engine/report.py`)

`build_report(journal, run_id, links, artifacts, pause=, session_slug=) -> RunReport`, the one model the CLI, MCP and UI
render:

* `run`: `summary(row, links, pause=, session_slug=)` with `run_links` (`run` `/api/runs/<id>`, `report`, `journal`,
  `events`, `plan` under it, `session` = `links.session(project, slug)`).
* `failures`: every instance with status `failed` or `soft_failed`, in `seq` order (`FailureOut` with node, instance,
  op, label, src, code, message, soft, the failed `CheckOutcome`s of a checkpoint failure, the failure shots of
  `node.failure_shot` events and checkpoint evidence shots, detail), then for a run that ended `error` or `aborted`
  without a failed node, one run-level failure (`node` `None`, `code` `run_error`/`run_aborted`, the reason).
* `checkpoints`: every `check.checkpoint` instance, from its output or (failed ones) `error.detail["checkpoint"]`,
  validated as `CheckpointOutcome`, shots as `ArtifactRef`s.
* `artifacts`: every artifact id of every instance in `seq` order (failure and pause shots included), as
  `artifact_ref(artifact, node=, instance=)` from the store; ids the store no longer has are left out.
* `values`: the run's outputs; `cache`, `resources`, `environment`, `issues` (the `run.warning` events) from the row
  and journal; `journal` = `JournalRef(run_id, nodes, events)`.
* `timings`: `total_ms` = finished (or now) - created; `queued_ms` = the `run.preparing` event - created; `preparing_ms` =
  the first `run.running` - `run.preparing`; `paused_ms` = the sum of `run.paused` -> next status intervals;
  `running_ms` = total - queued - preparing; `nodes` = the `SLOWEST_NODES` slowest instances by `duration_ms`.

`journal_page(...)` returns `JournalPage` (nodes, expansions, resources, and with `events=True` up to `limit` events after
`after`, `next_after` = the last id when more remain). `compare_runs(a, a_nodes, b, b_nodes) -> RunComparison`: instances
are matched by `(node_id, instance)`; a status difference, an output difference, an error code difference or a different
artifact count is a `Divergence`; instances present in one run only diverge on `status` with the missing side `None`
(design 6 `--repeat`). Outputs are compared by `value_digest` of a normalized copy, so artifact ids that differ only
because a run ingested the same bytes again do not diverge: an object that validates as `ir.types.Artifact` becomes its
`sha256`; then every string equal to one of the instance's own artifact ids (`NodeRecord.artifacts`), bare or as
`artifact:<id>`, becomes `artifact#<index of that id in NodeRecord.artifacts>` (checkpoint `shots`, `MediaInfo.artifact`
and `uri`). The `Divergence` carries the original outputs.

## 15. HTTP API (P11)

### 15.1 Authorization

* Reading a run (status, report, plan, journal, events, compare) needs viewer access to the run's project and session
  (`api.deps.check_level(db, principal, project_id, session_id, "viewer")`); starting, controlling, cancelling and
  replaying need editor access (`routes_leases.authorize_target(ctx, principal, project, session)` for new runs). Admins
  and the local principal pass. A run the principal may not see is `404 run_not_found`.
* Path-based planning executes code of the tree on the hub (entry files, plugin modules), so `authorize_tree(ctx,
  principal, tree, level)` (in `routes_plans.py`, used by every route that takes `tree`, `spec` or `source`) allows admins,
  and otherwise requires the tree's `[project] id` to be a project on which the principal has `level` access; a tree
  without a project id is admin only; inline `source` is admin only.
* `GET /api/runs` lists for non-admins only runs of projects and sessions their grants cover
  (`grants.scope_for_user`).

### 15.2 Endpoints

| Method and path | Body / query | Response | Errors |
| --- | --- | --- | --- |
| `POST /api/plans` | `PlanBuildIn` | `PlanOut` (plan JSON, validation, tree) | 400 `plan_build` (`BuildError`, with `where` = the user source line, as the `plan` CLI prints it) |
| `POST /api/plans/validate` | `PlanValidateIn` | `PlanValidation`; plan JSON that does not parse as `Plan` gives `ok=false` with one `plan.json` issue | |
| `POST /api/runs` | `RunCreate` (`wait` seconds) | 201 `RunCreated` (summary and admission warnings), after waiting up to `wait` for a terminal or paused state | 422 `RunRefused`; 400 `plan_build`; 403/404 |
| `GET /api/runs` | `project`, `session`, `status` (repeatable), `plan` (hash), `name`, `limit`, `cursor` | `RunPage` | |
| `GET /api/runs/{id}` | `wait` | `RunSummary` (after waiting up to `wait` for a terminal or paused state) | 404 |
| `GET /api/runs/{id}/report` | | `RunReport` | 404 |
| `GET /api/runs/{id}/plan` | | the admitted plan JSON | 404 |
| `GET /api/runs/{id}/journal` | `events`, `after`, `limit` | `JournalPage` | 404 |
| `GET /api/runs/{id}/events` | `Last-Event-ID` header | `text/event-stream` (section 8) | 404 |
| `POST /api/runs/{id}/control` | `ControlRequest` | `ControlResponse` | 404; 409 `run_finished` for a terminal run |
| `POST /api/runs/{id}/cancel` | `RunCancel` | `RunSummary` (after the run ended, up to 30 s) | 404 |
| `POST /api/runs/{id}/replay` | | 201 `RunCreated` (same plan, tree, params, options, backends, clock, fakes; `replay_of`) | 404, 422 |
| `GET /api/runs/{id}/compare/{other}` | | `RunComparison` | 404 |
| `GET /api/ops` | `family`, `tree`, `plugins` | `OpList` (`ir.schema.op_summary` plus the hub's executor: name, version, location, available, native) | |
| `GET /api/ops/{name}` | `tree`, `plugins` | `OpOut` with `schema` (`ir.schema.op_schema(spec, registry=)`) | 404 with suggestions |
| `POST /api/ops/{name}/do` | `DoRequest` | `DoResult` | 404, 422, 400 |
| `GET /api/flows` | `tree`, `kind`, `plugins` | `FlowList` | 403/404 |
| `GET /api/flows/source` | `tree`, `path` | `FlowSource` | 404 |
| `PUT /api/flows/source` | `FlowSave` | `FlowSaved` (validations per entry) | 409 `sha_mismatch`; 400 bad path |
| `DELETE /api/flows/source` | `tree`, `path` | `FlowSaved` (no entries) | 404 |

Error bodies follow `api.errors` (`{error, message, ...}`); `RunRefused` adds `issues`. `RunCreate.plan` is raw JSON so a
malformed plan becomes a refusal with a `plan.json` issue instead of a 422 request validation error.

`entry_path(tree, path)` (routes_flows) accepts only a relative POSIX path without `..` ending in `.py` under
`.harness/{flows,seeds,edits,scenes,scores}/` or `<dir>/.harness/flows/` of the tree, at most `MAX_SOURCE_BYTES`;
saving writes atomically (temporary file and rename), then `HubRegistries.invalidate(tree, path)` and plans every entry of
the file through `PlanBuilder` (flows of matrix apps with each platform), returning the validations keyed
`"<kind>:<name>"` (a load error is `error`, not an HTTP error). Deleting removes the file and invalidates.

### 15.3 Run manager (`engine/runs.py`)

`engine(ctx)` returns the `RunManager` service (`SERVICE = "engine"`). `RunManager.__init__` additionally (P11):
registers the built-in executors (`ops.register_builtin_executors(self.executors, self.auth,
api_timeout=engine.apiTimeoutSeconds)`), creates
`CacheService(ctx.paths.cache_dir, ctx.db, max_bytes=engine.cacheMaxGb GB, max_age_days=engine.cacheMaxAgeDays)` and
`SandboxRunner(limits=SandboxLimits(timeout_seconds=engine.sandboxTimeoutSeconds, cpu_seconds=timeout + 1,
memory_mb=engine.sandboxMemoryMb), scratch=ctx.paths.tmp_dir / "engine")`, and passes `devices=ctx.pools.devices.available`
to `Admission`. `runs.py` registers `@on_startup(order=45)` (`engine(ctx).recover()` and the maintenance thread) and
`@on_shutdown(order=5)` (`shutdown()`: abort active runs with the shutdown reason, release their resources, stop the
thread).

* `start(request, principal)`: run id `ids.new_ulid()`; `admission.admit`; ensure the project and the session rows
  (`projects.ensure`, `sessions.ensure_in_project`); `journal.create_run` (request JSON without the plan, environment,
  simulated, replay/repeat links); journal admission warnings (`run.warning`); publish `run.created`; register the
  run; dispatch (a run thread starts when fewer than `engine.maxRuns` runs are active; otherwise it waits `queued`
  and the dispatcher starts it when a slot frees, oldest first). Returns the summary.
* The run thread: `preparing`; with fakes, the fake driver provider joins this run's `DriverPorts` providers for every
  platform without a real one (`fakes.drivers`), and `FakeSeedHook`/`FakeCassetteHook` are passed to
  `planner.acquire(seed_hook=, cassette_hook=)` (`fakes.seeds`, `fakes.cassettes`), so fakes never leak into other runs;
  `planner.acquire`; build `SchedulerDeps` (journal writer, cache, a
  `ContentResolver(artifacts, tree=, base=plan.base, plugin_roots=entry.roots(), scratch=)`, plugin digests = the
  `TreeRegistry` and `digests` = its digest per plugin id, artifacts, resources, slots, `DriverPorts`, sandbox, a `Debugger(enabled=options.debug or
  breakpoints or break_on_error, status=..., breakpoints=, break_on_error=, journal=<a callback calling the run journal's
  event(type, detail=detail)>)`, `choose` over the run's registry and fake
  source); `Scheduler(...).run()`; set outputs, counts, status; release resources, close ports, `auth.forget(run_id)`;
  publish `run.finished`; dispatch the next queued run. A keepalive timer runs while resources are held. Every
  unexpected exception ends the run `error` with `engine: <type>: <message>`.
* `status`, `report`, `journal_page`, `list`, `compare` read the journal (`report.py`); `wait(run_id, timeout, until=)`
  blocks on the run's condition (default until terminal or paused); `control` journals the run event `control`
  `{action, actor}` and forwards to the active scheduler (409
  for a terminal run; a queued or preparing run answers `ok=False` for paused actions); `cancel` aborts (queued runs
  end at once); `replay` starts a new run from the stored plan and request; `do(op, request, principal)` builds a
  one-shot plan (`PlanBuilder.oneshot`), starts it, waits `request.wait`, and returns `DoResult` with the op's value
  (`report.values["value"]`), artifacts and failure.
* `recover()` marks every `journal.unfinished()` run `error` with `RECOVERY_REASON` and returns their ids; `maintain()`
  (every `MAINTENANCE_SECONDS`) prunes runs finished more than `engine.runRetentionDays` ago and evicts the cache.

## 16. Hub planning and registries

### 16.1 `PlanBuilder` (P9, `engine/build.py`)

* `build(BuildRequest) -> BuiltPlan`: with `spec`, the tree is `tree_of(spec, tree, cwd)` (the spec file's tree; an
  explicit `tree` must contain the file); `entry = registries.for_tree(tree, plugins=)`; `eks.workspace.plan_spec(spec,
  params=, platform=, cwd=, plugins=, registry=entry.registry)`; then `validate`. With `source`, the text is written to
  `<tree>/.harness/<folder>/_eks_inline_<sha256 of the source, 16 hex>.py` (a helper name discovery never loads), planned
  as a spec with `::entry` when given, and deleted in `finally`; one inline plan per tree at a time (`self._inline`);
  `src` lines of such plans point at the deleted file. Authoring errors (contracts-m0 12.1 list and the user-frame
  rule of decisions-m0 row 163) raise `BuildError(message, where=<file:line: code>)`.
* `validate(plan, tree=, plugins=) -> (TreeRegistry, issues)`: `eks.validation.validate(plan, registry=entry.registry,
  tree=tree)` plus `requirement_issues`, as `IssueOut` (`issues_of(report)`).
* `oneshot(OneShotRequest) -> BuiltPlan`: a plan of kind `flow` named `ONESHOT_NAME` traced, inside
  `using_registry(entry.registry)`, with a `TraceContext(kind="flow", name=ONESHOT_NAME, registry=, clock=, session=,
  tree=)`: when the op has resources, a `SessionSpec(key="main", app=, platform=, persona=, seed=, backend=, now=,
  root_seed=)` (app: given, else the tree's only app on the platform; platform: given, else the app's only platform;
  else `BuildError` naming what to pass; `now`, the root seed and the backend definition come from the seed as
  `eks.planning.inherited` gives them to flows), the clock `Clock(now=spec.now, seed=spec.root_seed or 0)`,
  `session.as` first when a persona is given, then `Session(ctx, spec).op(op, **args)` inside `tracing(ctx)` (shorthand
  targets work as in flows), outputs `{"value": <ref>}`; ops without resources are recorded with `ctx.record`. The
  plan is finalized (hashed) and validated like any other.
* `files(tree, kind=, plugins=)`: discovers every entry file of the tree (`eks.discovery.discover`, optionally only the
  folder of `kind`) and loads each in the tree registry (`for_tree`), returning one `EntryFile(path relative to the tree
  in POSIX form, kind of its folder, entries, error)` per file in discovery order: `EntryOut`s carry the parameters from
  the signature (name, annotation text, default as JSON when it has one), tags, description, app and platforms of
  flows; a file that fails to load has no entries and its error message (the route turns it into `FlowFileOut.error`).
  `entries(tree, kind=, plugins=)` flattens the entries of the files that loaded.

### 16.2 Hub registries (P5, `engine/registries.py`; BL-004, BL-007)

`HubRegistries.for_tree(tree, plugins=) -> TreeRegistry` (thread safe, LRU of `capacity` trees; calls for different
trees walk and hash in parallel, loading is serialized):

1. One scan of the tree (discovery's walk, cached per tree and revalidated by directory mtimes) gives the entry files and
   `project_digest`, the `tree_code_digest` of the tree: sha256 over the sorted relative paths and bytes of every `*.py`
   under every `.harness` directory of the tree except `.harness/plugins/**`, re-hashed only when the (path, mtime,
   ctime, size, inode) stamp of those files changes. It is computed before loading, so a file edited during the load
   gives a newer digest on the next call.
2. Plugin states: every active plugin with modules, its version and its digest (`record.digest`, else
   `plugins.trust.content_hash(record.root)` cached under a file stamp; `plugin_digest(record)` is the uncached rule).
3. The working registry `eks.discovery.tree_registry(tree, plugins=plugins, isolated=True)` (rebased on the built-ins
   only, never on whatever the hub process registered outside a tree, BL-007 S1) is private to the hub: inside
   `using_registry(working)`, `eks.discovery.load_plugins(tree, host=self.plugins, prune=True)` when `plugins` (S2) and
   the sync of the scanned seed and scene files (`sync_tree` semantics, S3).
4. The `generation` increases whenever a plugin digest, the project digest, the plugin set or the built-ins differ
   from the previous call; a changed digest forces the plugin's modules to be imported again into the working registry
   (the hub loads through `eks.discovery._load_plugins(tree, host, prune=True, digests={id: digest})`: every plugin
   module record remembers the digest it was imported at, and a plugin with a module that neither this registry nor a
   shareable record holds at the current digest is refreshed first: its records here are dropped, the bytecode cache
   of every `*.py` under its root is deleted and every `sys.modules` entry whose file lies under its root is removed,
   so the import compiles the current source; this covers scoped plugins, package plugins without a digest and
   modules another registry imported earlier) (BL-004). A new generation publishes a fresh
   copy of the working registry as `TreeRegistry.registry` (adopted by the tree's discovery caches, never mutated by
   `forget_file`, `invalidate` or later re-imports); while the generation stays the same, the same copy is returned. An
   entry handed out earlier therefore keeps the code its digests describe for the runs admitted with it; the replaced
   copy is released from the discovery caches.

`invalidate(tree, path)` drops the cached entry of that tree (both flags) and forgets the file's module in the tree's
working registries (`eks.discovery.forget_file`); `forget(tree)` drops everything of the tree
(`eks.discovery.forget_tree`). `requirement_issues(plan, entry)` returns `plugin.missing`/`plugin.version` issues for
`plan.requires` entries the tree registry does not satisfy (the validator's rule, run against the hub's loaded build).

`eks/discovery.py` and `eks/workspace.py` changes (P5):

* `tree_registry(tree, *, plugins=True, isolated=False)`: `isolated=True` keeps a separate cached registry per tree and
  flag, rebased on `builtins_registry()` (a registry holding only the `eks` provenance specs of the process registry,
  rebuilt when those change) instead of the process registry.
* `load_plugins(tree, *, host=None, prune=False)`: with `prune`, specs whose provenance plugin is neither `eks`,
  `project` nor an active plugin of the tree, and specs of modules an active plugin no longer lists in its manifest,
  are dropped from the active registry (`Registry.drop_module(module, plugin)` for each defining module) and their
  module records forgotten. Scoped plugin modules no module record uses any more (pruned, released, or replaced by a
  re-import under a new digest) leave `sys.modules`.
* `sync_tree(tree, *, kinds=None) -> list[EntrySpec]`: `load_tree` plus dropping, from the active registry, every entry
  module of this tree recorded in the load cache whose file no longer exists (`Registry.drop_module(name)` and the cache
  entry).
* `forget_file(path)`, `forget_tree(tree)`: drop load cache, plugin module records and seed cache entries of the
  registries of that file or tree; the caches `_FILES`, `_PLUGIN_MODULES`, `_TREES` and `eks.workspace._SEEDS` are bounded
  (at most 64 trees; evicting a tree evicts every per-registry entry of its registries) (S4).
* `workspace_registry(tree, *, plugins=True, isolated=False)` passes `isolated` through; `plan_spec(..., registry=None)`
  uses a given registry instead of building one.
* `isolated=False` keeps today's behaviour exactly (the CLI, `eks.plan` and every M0 test).

## 17. CLI (P12, `cli/run_cmds.py`)

* `eks-harness run <spec> [--set K=V]... [--platform P] [--backend REF [--backend-tree DIR] [--backend-key KEY]]
  [--project P] [--session S] [--break SPEC]... [--break-on-error] [--debug] [--repeat N] [--cache MODE] [--fake
  FAMILY]... [--no-plugins] [--no-wait] [--timeout S] [--json]`: builds the plan locally exactly like `plan` (the same
  authoring error handling and exit 2), validates it locally (exit 1 with the issues when invalid), and posts `RunCreate`
  with the plan JSON, `tree` (the spec's tree), `cwd`, `params` (the `--set` texts), the options (`--break`/`--break-on-
  error` imply `debug`), `--fake` as `FakeProfile(families=[...] or ["*"])`. It follows the run (`follow`: the event stream
  with reconnection from the last id) printing one line per node outcome (`<status> <op> <node> <src> <ms>`, cache hits
  marked) and finally the report (`render_report`: status, failures with source lines and shots, checkpoints,
  artifacts with links, values, timings). When the run pauses and stdin is a terminal, `debugger_prompt` shows the pause
  and reads `DEBUG_COMMANDS` (`continue`, `step`, `retry`, `skip [json]`, `abort`, `edit <json>`, `do <op> <json>`,
  `break <spec>`, `delete <id>`, `breaks`, `status`, `help`), sending `POST /control`; without a terminal it prints how
  to attach (`eks-harness debug <id>`) and exits `RUN_PAUSED` (12). `--repeat N` runs N times (later runs
  `repeat_of` the first), compares each with the first (`/compare`) and prints the diverging nodes; the exit code is the
  worst of the runs. `--no-wait` prints the run id. Exit codes (`exit_code`): passed 0, failed or error 1, aborted 13 (`RUN_ABORTED`; 11 already means nothing selected),
  paused without a terminal 12; plus the CLI's usual codes (2 usage, 3 daemon down, ...). `--json` prints the report
  JSON (and with `--repeat` `{"runs": [...], "comparisons": [...]}`).
* `eks-harness runs list|show|replay|control|cancel` as registered in the skeleton; `show` prints the report (and the
  journal or events with `--journal`/`--events`); `replay` follows the new run like `run`.
* `eks-harness debug <run>`: follows the run and opens the prompt whenever it pauses.
* `eks-harness do <op> [K=V]... [--args JSON] ...`: `parse_pairs` (values parsed as JSON when they parse, else text;
  `--args` merged under the pairs), posts `DoRequest`, prints the value (and artifacts with links), exit 0 when the op
  passed, 1 when it failed.
* `cli/__init__.py` `DESCRIPTION` lists exit code 12 ("run paused, attach with eks-harness debug").

## 18. MCP tools (P12, `mcp_engine.py`)

Every tool calls the hub through `HarnessTools.call` (stdio and HTTP servers alike); `tree` defaults through
`mcp_context.resolve_tree`; API errors become `ToolError` with the message and the issues.

| Tool | Does |
| --- | --- |
| `eks_run` | `POST /api/runs` with `spec` (relative to the tree, `cwd` = tree), `source` or `plan`, params, platform, project, session, `backend` (`BackendRef`), options (breakpoints, break_on_error, debug, cache), fakes (`FakeProfile(families=fakes)`), `instance = harness_instance(tree)`, `wait`; returns the report when the run ended, else `{run: summary, pause}` |
| `eks_plan` | `POST /api/plans`; returns `{plan, validation}` |
| `eks_validate` | `POST /api/plans/validate` |
| `eks_ops` | `GET /api/ops` or `/api/ops/{name}` |
| `eks_do` | `POST /api/ops/{op}/do` |
| `eks_run_status` | `GET /api/runs/{id}` (with `wait`), plus `/report` with `report=true` and `/journal?events=true&after=` with `events_after` |
| `eks_run_control` | `POST /api/runs/{id}/control` |
| `eks_flows`, `eks_flow_get`, `eks_flow_save`, `eks_flow_delete` | `/api/flows` endpoints |

`register_engine_tools` returns the registered names; the server instructions (`mcp_server.INSTRUCTIONS`) gain one
paragraph about runs (run ids, reports, pauses).

## 19. Fake executors (P3, `ops/fakes.py`)

A fake profile simulates what the hub cannot do yet; reports mark the run `simulated` and list the faked ops. Fakes
are deterministic: the same node (by hash), args and answers give the same values and byte-identical files.

* Coverage: `FakeExecutors.covered(op, real=)`: a `force` pattern covers it, or (without a real executor) a `families`
  pattern does. `executor(op)` returns `ExecutorSpec(FAKE_EXECUTOR, FAKE_VERSION, ops=(op.name,), run=self.run or
  container=self, resolver=self.resolve for machine-dependent ops, fake=True, locations=None)` or `None`.
* Answers: `answer_for(profile, node, attempt)` returns `answers[node.id]`, else `answers[op]`, else `None`, honouring
  `times` (the answer applies to attempts `1..times`). An answer with `fail` raises `NodeFailure(code, fail,
  retryable=, soft=)`; `delay_ms` sleeps (cancellable) first; `value` replaces the default value; `resolution` replaces
  the default resolution.
* Default values by output type (`default_value`): `none` -> `None`; `bool` -> `True`; `int` -> `1`; `float` -> `1.0`;
  `str` -> `ui.url`/`ui.wait_url` `FAKE_URL`, else `f"fake {op} {node.hash[7:15]}"`; `json` -> `ui.route` `{"name":
  "fake", "params": {}}`, `web.logs`/`mobile.route_names` `[]`, `expect.target` `{"ok": true, "assertion": <kind>}`,
  `check.checkpoint` a `CheckpointOutcome` dump (every check of the session's `defaults` and `checks` passing, one PNG
  shot), `check.explore` `{"ok": true, "screens": 0, "findings": []}`, `media.markers` `{"<source name>": {"<stream>":
  [<0.5 s grid of 16 points as times>]}}`, `check.media` `{"ok": true, "kind": <kind>}`, `data.create`/`data.base`
  `{"id": <derive_id("f", path)>, "path", "kind", "key"}`, otherwise `{}`; `artifact` -> a deterministic file ingested
  through the sink (`capture.shot`, `overlay.annotate`, checkpoint shots: PNG from `png_bytes(f"{op}:{node.hash}")`;
  `capture.dump`: text; `capture.har`: JSON; `capture.record` exit: a small JSON file of kind `video`); `media` -> a
  `MediaInfo` `{uri: "artifact:<id>", kind, artifact: <id>, width/height/fps/duration from the args when they have
  them}` with a deterministic JSON file of the media kind; `list[T]` -> `[]`; `resource:K` -> the `data.create` object.
  `default_value` has no sink, so it returns the value types only and raises `ValueError` for an `artifact` or `media`
  output (other than `media.source`): those values come from the files `FakeExecutors.run`/`exit` ingest and
  `FakeDriverPort.call` returns.
* `media.source` fakes do not read the file (content resolution is the engine's, 9.3) and return `{uri, kind}`.
* Containers (`capture.record`, `net.cassette`, `score.take`, plugin containers): `enter` runs the body branches;
  `exit` returns the default value of the op (a recording or take file, `None` for cassettes, calling the cassette
  hook's `opened`/`closed` when the run has one). The run's hook is `FakeExecutors(profile, registry,
  cassette_hook=None)`'s keyword (`self.cassette_hook`); without one it is a new `FakeCassetteHook` when
  `profile.cassettes`, else none. The run manager passes the same per-run hook it gives `acquire(cassette_hook=)`, so a
  replay sees what a record of the same run closed. The `CassetteRequest` key is `cassette_key(plan, node, identity)`
  with the identity of the backend of the cassette's session (`None` without one).
* `default_resolution(op, node, registry)` (`None` for nodes that are not machine-dependent) returns a record with
  every field of section 9.4 for every case (`backend`, `version`, `model`, `device`, `compute_type` per markers
  source; `engine`; `encoder`, `encoder_version`; `inference.*`; `gpu.*`; `platform`; `browser`; `device`, `model`,
  `weights`, `backend_version`), all `"fake"`-prefixed strings, so it satisfies any requirement; an answer's
  `resolution` replaces it (tests use this to produce incomplete or changing records).
* Worker ops are simulated through the port, not through fake executors: the real worker executor (section 20) routes
  them to the lease's port, and with `fakes.drivers` that port is a `FakeDriverPort` (so `families=["*"]` alone simulates
  every `ui.*`, `web.*`, `mobile.*`, `capture.*`, `check.checkpoint`, `expect.target`, `overlay.show`, `inject.*`,
  `fake.*`, `net.*` node and the `capture.record`, `net.cassette`, `score.take` containers). Fake executors serve the
  ops without a real executor at other locations (`media.*`, `data.*`, `edit.render`, `audio.mix`, `gfx.render`,
  `blender.render`, `scene.render`, `check.media`, `overlay.annotate`, plugin ops) and any op a `force` pattern names.
* `FakeDriverProvider(profile, registry, scratch)` / `FakeDriverPort(lease, session, run, profile, registry, scratch)`:
  `call(op, args, site=, phase=)` answers worker ops with the same defaults and answers, taking the node and attempt from
  `site` and the `OpSpec` from the registry (`session.as` returns `None`; files are
  returned as `DriverFile`s with role `value` for artifact and media outputs and `shot` for checkpoint shots, written
  under `scratch`), records `(op, phase)` in `calls`; `snapshot(kinds, dest)` writes a PNG per `screen` and an HTML file
  per `dom`/`a11y`; `version()` is `"fake-1"`; `simulated` is true. Args are validated inside
  `using_registry(self.registry)` (plugin catalog kinds resolve on any thread). Both take the keywords `cassette_hook=None,
  plan=None, backends=None` (`backends`: `BackendIdentity` by plan resource id, from the acquired resources); a hook
  without a plan raises `ValueError`. With a hook, `net.cassette` `enter` calls `hook.opened` (reply `detail["cassette"]`)
  and `exit` calls `hook.closed(request, {node, instance, attempt})` after the answer checks (reply
  `detail["cassette"]` = its return); the request is built like `FakeExecutors`' with `cassette_key(plan, node,
  identity)`, the identity being the backend resource of the lease's session (or in `node.uses`), else the only one in
  `backends`, else `None` (the rule of `acquire`). The run manager builds the provider after `planner.acquire` with the
  same per-run hook it passes to `acquire` and `FakeExecutors`, the plan and `{rid: handle.identity}` of the held
  backends; without a hook the port never touches cassettes.
* `FakeSeedHook.restore` returns `SeedRestoreResult(restored=True, snapshot=f"fake:{key[7:23]}")`; `FakeCassetteHook`
  keeps recorded cassettes in memory by key: `available` true after a `closed` of the same key (or for `record`/`auto`),
  `opened` returns `{"mode", "key"}`, `closed` returns `None`.

## 20. Real executors (P8, `ops/`)

`register_builtin_executors(registry, auth, api_timeout=)` registers `ApiExecutor(auth, timeout=api_timeout).spec()`
(`ops=API_OPS`), `BackendInfoExecutor().spec()`, `SessionAsExecutor(auth).spec()`, `ExpectValueExecutor().spec()`,
`WorkerExecutor().spec()` (`eks.worker`) and `WorkerExecutor().container_spec()` (`eks.worker.container`), the worker
specs with `ops=("*",)` and `locations={"worker"}`, all `version=BUILTIN_EXECUTOR_VERSION` and built-in provenance, and
returns the registry.

* `api.*` (`ops/api.py`): the backend handle (`call.backend()`); URL = `request_url(handle.url, args.path)` (the base
  URL's path is kept: `base.rstrip("/") + "/" + path.lstrip("/")`; a `path` with a scheme is refused with
  `args_invalid`); query = `query_params(args.query)` (scalars as text, `true`/`false` for booleans, lists repeated,
  objects as compact JSON, `None` left out); headers = `Accept: application/json`, `User-Agent: eks-harness/<version>`,
  `RUN_HEADER`/`NODE_HEADER`, `args.headers`, then the persona headers when there is a persona (`persona(call)`:
  `args.persona`, else `call.state.persona`, else none; `auth.headers(AuthRequest(...))`, `PersonaAuthError` fails the
  node); body = `args.body` as JSON for post/put/patch; timeout = `min(remaining, engine.apiTimeoutSeconds)` (the
  executor's `timeout`). Transport errors raise `NodeFailure("http_error", retryable=True)` (only `api.get` is
  idempotent, so only it retries); a status outside `args.expect_status` (any 2xx when unset) raises `NodeFailure(
  "http_status", detail={method, url, status, body excerpt})`; the output is `parse_body(response)`: JSON when the
  content type is JSON or the body parses as JSON, `None` for an empty body, else the text. The journal gets
  `node.http` `{method, url, status, ms, bytes, request excerpt, response excerpt}` with excerpts of `BODY_LIMIT_BYTES`
  and every secret string of the backend info (`pools.backend_info.secret_strings`) and every auth header value
  replaced by `<redacted>`. URLs never carry credentials.
* `backend.info`: the redacted info of the session's backend plus `{"backend": {id, definition, generation, plugin,
  version}}`.
* `session.as` (`ops/session.py`): sets `call.state.persona`; checks `auth.can_authenticate` and journals `{"api": true |
  false}`; then `call.driver.call("session.as", {"persona": persona, "headers": <auth headers when available>},
  site=CallSite(call.node, call.instance, call.attempt))` for
  the UI login (`DriverError("driver_missing")` without a port); output `None`.
* `expect.value` (`ops/expect.py`): `assertion_failure(assertion, value)` evaluates `Eq`, `Ne`, `Contains` (substring,
  list item, object key), `Matches` (`re.search` on text), `Gt`/`Ge`/`Lt`/`Le` (numbers; JSON numbers only), `Truthy`,
  `Falsy` (`values.truthy`), `Length` (`HasCount`-like bounds on `len`) and returns a message or `None`. Without
  `eventually` a failure raises `NodeFailure("assertion", message or args.message, detail={value, assertion}, soft=args.soft)`.
  With `eventually`, it polls `call.reread(node.args["value"])` with backoff from `POLL_START_SECONDS` to
  `POLL_MAX_SECONDS` until the assertion holds or `args.timeout` (else the session timeout, else
  `DEFAULT_EVENTUALLY_SECONDS`) passes. Output `{"ok": true, "value": <value>}`.
* Worker routing (`ops/worker.py`): `run` calls `call.driver.call(op, call.data, site=CallSite(call.node,
  call.instance, call.attempt), phase="run", timeout=call.remaining())` and ingests the reply files (4.8); containers
  call `phase="enter"` (branches `("body",)`) and `phase="exit"`; no port raises `DriverError("driver_missing")`;
  `DriverError`s from the port pass through, other port exceptions become `driver_error`. Reply files are ingested
  through `call.artifacts.ingest(path, kind=, name=, caption=)`: role `value` builds the output (an `Artifact` dump by
  alias for `out == "artifact"`, `MediaInfo` for `out == "media"`), role `shot` appends the artifact id to
  `value["shots"]`, role `evidence` only ingests; every ingested id is in `ExecResult.artifacts` through the sink.

## 21. Settings (`config.py`, already added)

| Key | Default | Meaning |
| --- | --- | --- |
| `engine.maxRuns` | 4 | runs that execute at once |
| `engine.parallelism` | 4 | nodes of one run at once |
| `engine.retries` | 2 | automatic retries of idempotent nodes |
| `engine.nodeTimeoutSeconds` | 120 | default node timeout |
| `engine.apiTimeoutSeconds` | 30 | `api.*` request timeout |
| `engine.pauseTimeoutSeconds` | 1800 | pause timeout |
| `engine.sandboxTimeoutSeconds` | 30 | dynamic function wall clock limit |
| `engine.sandboxMemoryMb` | 1024 | dynamic function memory limit |
| `engine.runRetentionDays` | 30 | journal retention |
| `engine.cacheMaxGb` | 20 | cache size limit |
| `engine.cacheMaxAgeDays` | 30 | cache entry lifetime |

## 22. Backlog items

| Item | Where | Test |
| --- | --- | --- |
| BL-001 analyses resolved per machine | 9.2 `resolution` and `inputs`, 9.4, 4.6 resolvers, 9.7 steps 2 and 5, fakes 19 | P4 `test_engine_cache_keys.py` (unpinned markers with a fake resolution; a changed resolution changes the key; never served by node hash alone; a resolver failure fails the node), P10 `test_engine_scheduler_cache.py` (journaled resolution, `resolution_incomplete`, `resolution_mismatch`) |
| BL-002 mutable artifacts | 9.3 content resolution, 5.3 step 4, journal `resolution.content` | P4 (re-rendered artifact changes the key; a pinned digest mismatch raises `content_mismatch`), P10 (a pinned mismatch fails the media.source node; the sha256 is journaled) |
| BL-003 executor versions and fonts | 4.6, 9.2 `executor` | P3 (registry rejects empty versions), P4 (key changes with version, runtime version, harness digest and fonts digest for `fonts=True`) |
| BL-004 plugin code digests | 9.2 `plugins`, 16.2 steps 3 and 4, admission 3.3 | P5 `test_engine_registries.py` (same id and version with other code: other digest, other generation, modules re-imported), P4 (key changes with the digest; a plugin curve inside a pure render; project code digest) |
| BL-005 auto-chosen backends | 10.2, 10.3, 10.4 keys | P6 `test_engine_resources.py` (identity journaled for each source; plugin version mismatch fails; seed and cassette keys change with the identity) |
| BL-006 checked outputs | 6 `output_problem`, 5.3 step 9 | P1 `test_engine_values.py` (every type), P10 (a fake answer of the wrong type fails the node with `output_type`, nothing journaled as output, nothing cached, pause on error catches it) |
| BL-007 registry leaks | 16.2 | P5 (S1 to S4 repro scenarios as tests) |
| BL-008 ML effects in renders | 9.9 IR additions, 9.4 `inference.*` | P4 (each of the five effects makes the render machine-dependent; a plugin effect with `device_dependent=True`; the key adds the inference record) |
| BL-009 Blender keys | 9.4 Blender rows, 4.6 `runtime_version` | P4 (unpinned CPU Cycles needs `platform`; GPU needs `gpu.*`; runtime version enters the key) |
| BL-010 external tempo | 3.4, 9.2 `clock` | P9 `test_engine_admission.py` (refused without a clock, admitted with one), P4 (the run clock enters the key) |

## 23. Testing

Each package tests what it owns with real collaborators where they exist (a real `Database` and daemon `ctx` from the
fixtures, fake pools, real HTTP servers on `127.0.0.1:0` in a thread, real subprocesses for the sandbox) and fakes only
what the milestone fakes. The end-to-end acceptance test (P11, `tests/engine/test_engine_e2e.py`):

1. A tree under `tmp_path`: `.harness/project.toml` (`[project] id = "acme/e2e"`), `web-app/.harness/app.toml`
   (`[app] name = "web-app"`, `platforms = ["web"]`, `[web] url`, `timezone = "Europe/Istanbul"`, `locale = "tr-TR"`),
   `.harness/seeds/standard.py` (an `@eks.backend("testapi")` class implementing the `Backend` protocol, a persona
   resource kind and `@eks.seed("standard", backend="testapi", now=...)` creating the persona `admin`),
   `.harness/flows/e2e.py` (`@eks.flow(app="web-app", seed="standard", as_="admin")`: `s.api.get("/api/branches")`, a
   `ui.nav` with an f-string of a value from it, `expect(by.text(...)).visible()`, `s.checkpoint("list")`, `shot =
   s.shot("branches")`, `eks.annotate(shot, [eks.overlay.callout(...)])`, `s.api.post(...)` with `expect_status=201`, a
   `with eks.when(...)` on an API value, returning `{"branches": ..., "annotated": ...}`), `.harness/edits/clip.py` (an
   edit over `e.media("assets/clip.mp4")`, a small binary file the test writes to `.harness/edits/assets/clip.mp4`
   (relative media paths resolve against the authoring file's directory: the plan's URI is `file:assets/clip.mp4`
   and `plan.base` is `.harness/edits`, which content resolution joins; faked media executors never decode it), with
   `render_settings(encoder="libx264")`). `tests/eks/cli_fixtures` is a working template for the project file, app
   profile, persona resource kinds, seed and flow.
2. A `ThreadingHTTPServer` on `127.0.0.1:0` implementing `/api/branches` (GET, POST) with HTTP Basic auth for `admin`.
3. The backend instance pre-acquired through `POST /api/backend-leases` (definition `testapi`, the tree) and its info
   pointed at the test server with `ctx.pools.backends.store_info(record, {...url, credentials: {"admin": {"username":
   "admin", "password": ...}}})`.
4. `POST /api/runs` (TestClient) with the flow's plan built by `eks.workspace.plan_spec`, `tree`, `backend={"ref": <the
   bl_ lease id of step 3>}`, `fakes={"families": ["*"]}` and `wait=60`: status `passed`; the test server saw the GET and
   POST with the Basic header; the journal has
   every node instance with `ui.*`, `check.*`, `capture.*`, `overlay.*` simulated and `api.*` real; the event stream
   (`GET /events`) replays `run.preparing` ... `run.passed` with increasing ids; the report has the checkpoint, the shot and
   annotation artifacts with links, `values.branches` from the server, timings and `cache.stored >= 1`.
5. A second identical run: `overlay.annotate` and the edit plan's `media.source` and `edit.render` are cache hits
   (`node.cache` events with `outcome: hit`, `report.cache.hits >= 1`, the same output artifact ids), while `api.*` ran
   again.
6. The edit plan runs through `POST /api/runs` twice with the same expectations; `GET /compare` of the two flow runs
   reports no divergence other than API outputs that differ by design (the POST creates a new id): every `Divergence` is
   an `output` divergence of an `api.*` node.
7. After the test rewrites `assets/clip.mp4` with other bytes, a third edit run misses on `media.source` and
   `edit.render` (their keys differ from the second run's) and journals the new sha256 in `resolution.content`
   (BL-002).

## 24. Work packages

| Id | Wave | Owns |
| --- | --- | --- |
| P1 core values and debugger | 1 | `engine/model.py`, `engine/errors.py`, `engine/values.py`, `engine/debugger.py` |
| P2 journal, events, report | 1 | `engine/journal.py`, `engine/events.py`, `engine/report.py`, `db/migrations/0007_engine.sql` |
| P3 executors, drivers, fakes | 1 | `engine/executors.py`, `engine/drivers.py`, `ops/fakes.py` |
| P4 cache and IR keys | 1 | `engine/cache.py`, `ir/hashing.py`, `ir/registry.py`, `ir/catalog/effects.py` |
| P5 hub registries | 1 | `engine/registries.py`, `eks/discovery.py`, `eks/workspace.py` |
| P6 resources and auth | 1 | `engine/resources.py`, `engine/auth.py`, `renderq.py` |
| P7 dynamic sandbox | 2 | `engine/sandbox.py`, `engine/sandbox_child.py` |
| P8 real executors | 2 | `ops/__init__.py`, `ops/api.py`, `ops/session.py`, `ops/expect.py`, `ops/worker.py` |
| P9 hub planning and admission | 2 | `engine/build.py`, `engine/admission.py` |
| P10 scheduler | 3 | `engine/scheduler.py` |
| P11 run manager and HTTP API | 4 | `engine/runs.py`, `api/routes_plans.py`, `api/routes_runs.py`, `api/routes_ops.py`, `api/routes_flows.py`, `api/schemas_engine.py`, `daemon/app.py`, `config.py` |
| P12 CLI and MCP | 5 | `cli/run_cmds.py`, `cli/__init__.py`, `mcp_engine.py`, `mcp_server.py` |

Each package also owns its tests (`tests/engine/test_engine_<area>*.py` as named in its spec). Packages run in parallel
in one tree, so they do not edit `docs/dsl/decisions-m1.md`: every new choice goes into the package result as a
decision row (topic | decision | reason) and the integration step appends it. `docs/dsl/contracts-m1.md` is edited only
for a genuine contract error, with one exact-string edit made right after re-reading that passage, and the change is
stated in the package result.
