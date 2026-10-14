# Drivers and the typed op protocol

Worker ops (every op with location `worker`: `ui.*`, `web.*`, `mobile.*`, `expect.target`, `check.checkpoint`,
`capture.*`, live `overlay.*`, `net.*`, `inject.*`, `fake.*`) run in a Node driver worker attached to a leased
browser or device. The engine talks to the worker through one typed protocol: each node becomes one `POST /op`
call whose arguments are validated against a JSON Schema generated from the op's Python argument model. This page
describes the pieces, the protocol, targets, the native surface, app runtimes, worker extensions and process
ownership. The op list is in [reference/ops.md](reference/ops.md).

## Pieces

| Piece | Where | Role |
| --- | --- | --- |
| Port and provider | `eks_harness/drivers/port.py` | `WorkerDriverProvider` opens a port per session and lease; `WorkerDriverPort` sends ops, maps errors and routes mobile captures |
| Wire form | `eks_harness/drivers/wire.py` | `to_wire` (argument models to wire JSON), `wire_schemas` (one JSON Schema per worker op and platform), `typescript` (renders `ops.d.ts`) |
| Worker manager | `eks_harness/drivers/workers.py` | one worker per lease, spawned on a free port, reused while its code and configuration match |
| App runtimes | `eks_harness/drivers/runtime.py` | `@eks.app` hooks, `AppContext`, `AppRuntime`; the app profile's settings when no runtime exists |
| Native surface | `eks_harness/drivers/native.py` | system dialogs and native apps: Android through uiautomator and adb, iOS through idb |
| Device captures | `eks_harness/drivers/devices.py` | screenshots and recordings of simulators and emulators through the hub's device pool |
| Cassettes | `eks_harness/drivers/cassettes.py` | recorded traffic under `<state>/cassettes/<key>/cassette.json` |
| Process registry | `eks_harness/drivers/procs.py` | owner tracking and reaping of workers and dev servers |
| Kit | `workers/kit/src/ops.mjs`, `schema.mjs`, `targets.mjs`, `lifetime.mjs` | the op registry and `defineOp`, the schema validator, Target resolution, parent watch and idle exit |
| Generated types | `workers/kit/src/ops.d.ts` | TypeScript declarations of every wire argument model; a test keeps the file current |
| Web worker | `workers/web-driver/src/ops.mjs`, `server.mjs` | the web ops over a leased Chrome profile with Playwright |
| Mobile worker | `workers/mobile-driver/src/ops.mjs`, `server.mjs` | the iOS and Android ops through the React Native bridge and the native surface |
| React Native bridge | `sdk/react-native` (`@eks-harness/react-native`) | runs inside dev builds: strict target matching, actions, store, overlays, the fetch and XHR network shim, fakes |

## From a node to a worker

1. When a run prepares, the resource planner acquires the session's lease and, when the session needs one, its
   backend. The driver port opens at the same time: the app runtime's `prepare` and `boot` run and the worker
   starts, so app start-up does not count against the first node's timeout. A port that cannot open ends the run
   with `driver_error`.
2. The engine writes the wire schemas of the platform to `<state>/ops-<platform>.schema.json` and names the file in
   the worker configuration (`opsSchemaFile`). The worker loads it at start.
3. For each worker node, `to_wire` converts the validated arguments (below) and the port sends `POST /op`.
4. The reply's value is checked against the op's output type; files the op wrote are ingested as artifacts.

## Wire protocol

Request, `POST /op`:

```json
{
  "op": "ui.tap",
  "phase": "run",
  "args": {"target": {"by": "test_id", "args": {"id": "save"}}, "settle": true},
  "site": {"node": "n5c8w2k1d9q", "instance": "...", "attempt": 1, "src": "flows/x.py:12", "label": null},
  "session": {"key": "main", "platform": "web", "app": "web-app", "persona": null, "viewport": null,
              "noise": [], "timeoutMs": 10000},
  "resources": {},
  "deadlineMs": 1760000000000,
  "outDir": "/path/the/engine/created/for/this/call"
}
```

* `phase` is `run` for leaf ops and `enter` or `exit` for container ops such as `capture.record` and
  `net.cassette`.
* `resources.cassette` (`{dir, mode, key, name}`) is set on `net.cassette` enter from the hub's cassette store.
* Files an op writes go into `outDir`.

Reply: `{ok: true, value, files: [{path, role, kind, name, caption}], detail}` or
`{ok: false, error: {code, message, detail, retryable}}`. File roles are `value`, `shot` and `evidence`.

Other routes: `POST /snapshot {kinds: ["screen" | "dom" | "a11y"], outDir}` returns `{files}` for failure shots and
debugger pauses; `GET /health` returns `{kind, version, platforms, ops, pid, startedAt}` (`version` is the worker
code hash); `GET /events` streams worker events.

Error codes a worker returns: `target_not_found` (with near misses), `target_ambiguous` (with up to 10
candidates), `target_unsupported`, `op_unsupported`, `args_invalid`, `navigation`, `timeout`, `assertion`,
`check_failed`, `login_unsupported`, `native_unavailable` and `driver_error`. The port turns them into node failures;
transport and HTTP errors are `driver_error`. Retryable errors are retried only on idempotent ops.

## Wire form

`to_wire(spec, args, session, registry, clock)`:

* `Time` durations become integer milliseconds. An anchored time (a beat, a cue) is `args_invalid` on a worker op.
* A `Target` stays `{by, args, within, near, nth, surface}`, with `surface` `app` (default) or `native`.
* Preset names become their values: `pace`, `viewport` (`{name, width, height, scale, mobile, touch}` from the
  app profile's viewports or the presets), the `net.shape` profile and overlay `style`.
* `fake.arm` media refs become `{path, mime}` of the ingested file.
* `check.checkpoint` without `checks` gets the session's resolved set: the platform defaults plus the session's
  extra checks.
* Everything else is the JSON dump of the model.

## Targets

* Web: `test_id` matches `[data-testid=id]`, `text` exact by default (`exact=False` for a substring), `role` with
  an accessible `name`, `label`, `placeholder`, `css`, `point` (coordinates). `within` scopes the search, `near`
  picks the closest by box centre, `nth` indexes (-1 is the last). Only visible elements match unless the op says
  otherwise.
* Mobile (bridge): `{testID, text, label, placeholder, exact, kind, nth, near}`, `within` scopes to a subtree,
  `point` taps coordinates, `css` is `target_unsupported`. Text matching is exact by default (case and accents
  count); `exact=False` folds case and accents. Nested matches collapse to the outermost element.
* Strict everywhere: zero matches wait until the deadline, then `target_not_found`; more than one match without
  `nth`, `first` or `last` is `target_ambiguous`.

## Native surface

`by.text("Allow While Using App").native` (a target with `surface="native"`) reaches system permission prompts,
OS sheets and native apps. Android uses `uiautomator dump` for the tree and `adb shell input` for actions; iOS uses
`idb ui describe-all` and `idb ui tap|swipe|text` and answers `native_unavailable` when idb is missing. Supported on
the native surface: `ui.tap`, `ui.long_press`, `ui.fill`, `ui.type`, `ui.swipe`, `ui.exists`, `ui.visible`,
`ui.text`, `ui.wait_for`, `ui.wait_gone`, `ui.tree` and `expect.target`. `adb input text` types ASCII only; other
text on the Android native surface is refused with `native_unavailable`.

On mobile the port routes `capture.shot`, `capture.record`, `capture.hold` and checkpoint shots to the hub's device
pool, with the worker preparing the screen; `ui.swipe`, `ui.drag`, `ui.deeplink`, `ui.open_url` and any op with a
native, `point` or `rect` target run on the native surface. An app profile with `[mobile] bridge = false` is driven
entirely on the native surface; bridge-only ops answer `op_unsupported` there.

The session's permission policy (`@eks.flow(permissions="grant" | "deny" | "ask")`) grants or revokes the app's
runtime permissions before launch with `adb shell pm grant|revoke` and `xcrun simctl privacy`. `[mobile]
ios_bundle` and `android_package` name the app for it.

## App runtimes

```python
import eks
from eks_harness.drivers.runtime import AppContext, AppRuntime, PersonaLogin, RuntimeWorker


@eks.app("web-app")
class WebRuntime:
    def prepare(self, ctx: AppContext) -> AppRuntime:
        ctx.spawn(["pnpm", "dev", "--port", "3000"], key="web-app-3000", cwd=ctx.root,
                  ready_url="http://127.0.0.1:3000/")
        return AppRuntime(url="http://127.0.0.1:3000", api_url=ctx.backend_url)

    def login(self, ctx: AppContext, worker: RuntimeWorker, persona: PersonaLogin) -> None:
        worker.call("ui.nav", {"route": "/login"})
```

* `prepare(ctx)` starts what the app needs and returns `AppRuntime(url=, api_url=, options=, env=)`; `options`
  merge into the worker configuration.
* `boot(ctx, worker)` installs, launches or deep-links after the worker is attached.
* `login(ctx, worker, persona)` signs a persona in; `session.as` (`as_=`, `s.as_(...)`) calls it and answers
  `login_unsupported` without it.
* `AppContext` carries `app`, `platform`, `tree`, `root`, `profile`, `lease`, `session`, `run`, `backend`,
  `backend_url`, `backend_info`, `persona(key)`, `device` (udid or serial), `native` (its native surface, for
  example `ctx.native.dismiss_alerts()`), `state_dir`, `log` and `spawn(argv, key=, cwd=, env=, ready_url=,
  ready_log=, timeout=)`. Processes started with `spawn` are registered with the run's owner, keyed by `key`, and
  reused across runs while their owner lives.
* Without an `@eks.app` for the profile, the runtime comes from `app.toml`: `[web] url` and `api_url`, `browser`
  (`lease`, or `launch` for a local Chromium), `[mobile] metro_port`, `bridge`, `ios_bundle`, `android_package`.
  `[backend]` names the backend a flow of the app uses when the flow does not.

[examples/reference/.harness/plugins/reference/reference_apps.py](../examples/reference/.harness/plugins/reference/reference_apps.py)
has three runtimes: a Vite web app, a native Android app and an Expo app with the bridge.

## Worker ops in JavaScript

A plugin adds a worker op in two halves. The Python half declares the op and its argument model:

```python
import eks


@eks.op("acme", name="sign_in", location="worker", platforms=["web"], description="Sign in on the Acme login page")
class SignIn(eks.Args):
    user: str
    password: str
```

The JS half implements it in a driver extension listed in the plugin manifest:

```toml
[[contributes.driver_extension]]
path = "web/acme.mjs"
platforms = ["web"]
```

```js
export default function extend(kit) {
  kit.defineOp({
    name: 'acme.sign_in',
    platforms: ['web'],
    async run(args, ctx) {
      const page = kit.activePage()
      await page.fill('#user', args.user)
      await page.fill('#password', args.password)
      await page.click('button[type=submit]')
      return null
    },
  })
  return {}
}
```

The worker validates `args` against the schema generated from `SignIn` before `run` is called. A container op
defines `enter` and `exit` instead of `run`. The op context has `op`, `phase`, `site`, `session`, `resources`,
`platform`, `deadline`, `remaining()`, `outDir`, `file(name)` (a path in `outDir`), `addFile({path, role, kind,
name, caption})` and `log`, plus the worker's own members (the page on web, the bridge and native surface on
mobile). Throwing `OpError(code, message, {detail, retryable})` replies with that error; any other exception is
`driver_error`.

`extend(kit)` may also return hooks: `ops(defineOp, platformApi)` (another way to register ops), `onContext`,
`onPage`, `onRestore`, `onRecordingStart`, `status`, `apiHeaders`, `onBridge`, `onEvent` and
`onBackendChange(next, previous)` (the linked backend was replaced; log in again with `next.credentials`). `kit`
carries `config`, `settings` (the plugin's resolved settings), `id`, `log`, `emit`, `daemon`, `backend()` (the
linked backend's info) and `defineOp`; the web kit adds `activePage`, `ensureBrowser`, `settle`, `gotoPath`,
`locate`, `resolveTarget`, `persist` and `restore`, and the mobile kit adds `bridgeCall`, `pacedBridge`,
`fillInput`, `armFake`, `clearFakes`, `captureDevice` and `stateDir`.

## Worker configuration

The manager writes `config.json` (mode 600) into the worker's state folder and starts `node server.mjs` with
`EHX_WORKER_CONFIG` pointing at it. The worker binds a free port on `127.0.0.1` and writes `worker.json` with its
port and pid. Besides the app settings (URL, locale, time zone, viewports, pace, extensions, backend link) the
configuration carries `platform`, `opsSchemaFile` and `opsSchemaHash`, and the session's `timezone`, `locale`,
`now`, `permissions` and `networkIgnore`. A worker is reused for the next run on the same lease while its code
hash and configuration hash stay the same.

## Process ownership

Every process the harness spawns has an owner and dies with it.

* Workers and app dev servers start in their own process group; the owner kills the group on exit, in `atexit`
  and in its SIGTERM and SIGINT handlers.
* Workers watch their owner (`EHX_OWNER_PID`, a stdin pipe) and exit when it is gone or after
  `EHX_IDLE_EXIT_MS` without a request (`workers/kit/src/lifetime.mjs`).
* Each process is registered under `<state>/procs/<id>.json` with its pid, process group, start time, owner pid
  and owner start time, tree and install path. The hub reaps entries whose owner is dead at startup and on its
  housekeeping timer.
* `eks-harness reap [--all-installs] [--include-dev] [--dry-run] [--yes]` (`eks-harness doctor
  --kill-orphans` does the same as `reap --all-installs`) lists orphans and stops them after confirmation; with `--all-installs` it also finds workers and
  dev servers of other installs that were re-parented to init.

Nothing the harness starts runs headless: pool Chrome refuses `--headless`, emulators keep their window, and a
simulator lease fails when the simulator window cannot be shown.
