# M2 contracts: drivers

M2 makes the worker ops of the plan run on real browsers and devices: a typed op protocol between the engine
and the Node driver workers, every worker op on web, iOS and Android, app runtimes, process ownership (BL-017) and
native dialogs (BL-011). It builds on contracts-m1.md section 4.8 (`DriverPort`) and section 20 (`WorkerExecutor`).
Old flow APIs keep working until M7; nothing here removes them.

## 1. Module map

| Module | Owns |
| --- | --- |
| `eks_harness/drivers/wire.py` | wire form of op args (`to_wire`), wire JSON Schemas of worker ops, TypeScript declarations |
| `eks_harness/drivers/port.py` | `WorkerDriverProvider`, `WorkerDriverPort` (the real `DriverProvider`/`DriverPort`) |
| `eks_harness/drivers/runtime.py` | app runtimes: `@eks.app` hooks, `AppContext`, default runtime from the app profile |
| `eks_harness/drivers/procs.py` | process registry, ownership checks, reaping |
| `eks_harness/drivers/native.py` | native surface on Android (uiautomator, adb input) and iOS (idb or simctl), permission policy |
| `eks_harness/drivers/devices.py` | `DeviceCapture`: device screenshots and recordings through the hub's device pool |
| `eks_harness/drivers/cassettes.py` | `FileCassetteHook`: cassettes under `<state>/cassettes/<key>/cassette.json` |
| `eks_harness/cli/reap_cmds.py` | `eks-harness reap` |
| `workers/kit/src/ops.mjs` | op registry, `defineOp`, `/op` and `/snapshot` routes, typed errors, file handoff |
| `workers/kit/src/schema.mjs` | JSON Schema subset validator |
| `workers/kit/src/targets.mjs` | Target model to Playwright locators (web) and to bridge targets (mobile); strict matching |
| `workers/kit/src/lifetime.mjs` | parent watch and idle exit |
| `workers/kit/src/ops.d.ts` | generated declarations of every wire args model (checked in, a test keeps it current) |
| `workers/web-driver/src/ops.mjs` | web implementations of the worker ops |
| `workers/mobile-driver/src/ops.mjs` | iOS and Android implementations |
| `sdk/react-native/src/network.ts` | bridge fetch and XHR shim for `net.*` on mobile |

## 2. Wire protocol

* `POST /op` body: `{op, phase: "run"|"enter"|"exit", args, site: {node, instance, attempt, src, label},
  session: {key, platform, app, persona, viewport, noise, timeoutMs}, resources: {cassette?: {dir, mode, key, name}},
  deadlineMs, outDir}`. `resources.cassette` is set on `net.cassette` enter by the port from the hub's cassette hook. `args` is the wire form (section 3). `outDir` is a
  directory the engine created for this call; files the op writes go there.
* Reply 200 `{ok: true, value, files: [{path, role, kind, name, caption}], detail}` or
  `{ok: false, error: {code, message, detail, retryable}}`; transport and HTTP errors are `driver_error`.
* `POST /snapshot {kinds: ["screen"|"dom"|"a11y"], outDir}` returns `{files}` for failure shots and pauses.
* `GET /health` returns `{kind, version, platforms, ops: [names], pid, startedAt}`. `version` is the worker code hash.
* Error codes a worker may return, added to `engine.errors.ERROR_CODES`: `target_not_found`, `target_ambiguous`
  (more than one match without `nth`/`first`/`last`; detail lists up to 10 candidates), `target_unsupported`,
  `op_unsupported` (op not registered for the platform), `args_invalid` (wire schema), `navigation`, `timeout`,
  `assertion`, `check_failed`, `login_unsupported`, `native_unavailable`, `driver_error`. The port maps them to
  `NodeFailure(code, message, detail, retryable)`.
* The worker validates `args` against the wire schema of the op before running it.

## 3. Wire form

`to_wire(spec, args, session, registry, clock)` converts a validated args model:

* `Time` durations become integer milliseconds (resolved with the static clock context; an anchored time is
  `args_invalid`).
* `Target` stays `{by, args, within, near, nth, surface}`; `surface` is `"app"` (default) or `"native"`.
* Preset names (`pace`, `viewport`, `net.shape` profile, overlay `style`) become their values: `Pace` and `NetProfile`
  dumps, viewports `{name, width, height, scale, mobile, touch}` from `SessionSpec.viewports` or the viewport presets.
* `media` refs of `fake.arm` become `{path, mime}` of the ingested artifact file.
* `check.checkpoint` with `checks` unset gets the resolved set `[*SessionSpec.defaults, *SessionSpec.checks]`.
* Everything else is the JSON dump of the model.

`wire_schemas(registry, platform)` returns `{op: JSON Schema}` of the wire form for every worker op of the
platform; the engine writes it to `<worker state>/ops.schema.json` and the worker loads it at start.
`typescript(registry)` renders `ops.d.ts`.

## 4. Kit op loop

`defineOp({name, platforms, run(args, ctx), enter, exit})`; the driver servers register their built-in ops, plugin
driver extensions register theirs the same way (the extension `kit` has `defineOp`). `ctx`: `site`, `session`,
`deadline` (ms epoch), `outDir`, `file(name)` (a path in `outDir`), `step(entry)` (step log), `settle(opts)`,
`page` (web) or `bridge` (mobile), `native` (mobile). An op that throws `OpError(code, message, detail, retryable)`
replies with that error; any other exception is `driver_error` with the message.

## 5. Targets

* `web`: `test_id` -> `[data-testid=id]`, `text` (exact by default, substring with `exact=false`), `role`
  (+ `name`), `label`, `placeholder`, `css`, `point` (coordinates), `within` (scope), `near` (closest by box
  centre), `nth` (index; -1 is the last). Visible elements only unless the op says otherwise.
* `mobile`: the bridge target `{testID, text, label, placeholder, exact, kind, nth, near}`; `within` scopes the
  search to the subtree; `point` taps coordinates; `css` is `target_unsupported`.
* Strict: zero matches waits until the deadline then `target_not_found` (with near misses); more than one match
  without `nth` is `target_ambiguous`.
* `surface: "native"` routes the op to the native layer (section 8).

## 6. Ops

Web implements every worker op of the registry for `web`, mobile every op for `ios`/`android`, except:
`check.explore` (M6) and `score.take` (M5) answer `op_unsupported`.

* Actions honour `real` (trusted input), `settle` (wait for network and DOM quiet after the action; default true),
  `timeout`, and recording pace while a `capture.record` is open.
* `ui.settle(strict=true)` raises `timeout` when the page does not settle; `strict=false` returns.
* `ui.nav` uses the profile's navigation strategy (client router push with full-load fallback); mobile navigates by
  route name with params.
* Reads (`ui.text`, `value`, `attr`, `count`, `exists`, `visible`, `tree`, `url`, `route`, `state`, `layout`) return
  values of the op's output type.
* `expect.target` evaluates `IsVisible`, `IsHidden`, `IsEnabled`, `HasText`, `HasCount`, `HasValue`, ... with
  `eventually` polling until the timeout; a failing assertion is `assertion` (soft flag kept); output
  `{"ok": true, "assertion": kind}`.
* `check.checkpoint` runs the node's resolved check set (`checks` or `SessionSpec.defaults` + `checks`):
  `console_errors`, `page_errors`, `failed_requests`, `layout_spill`, `horizontal_scroll`, `broken_images`,
  `error_toasts` on web; `app_errors`, `route` and `error_toasts` on mobile; a kind the worker does not implement
  is a failed `check_unsupported` outcome. Windows are since the previous checkpoint of the session. Output is a
  `CheckpointOutcome`; the executor raises `check_failed` when `ok` is false and the node is not soft.
* `capture.shot`, `capture.dump`, `capture.har` write files with role `value`; `capture.record` enter starts the
  recording, exit stops it and returns the video file (role `value`, kind `video`); `capture.hold` sleeps only while
  a recording is open.
* `overlay.show`, `overlay.remove`, `overlay.clear` draw live overlays (web overlay script, mobile bridge
  `annotate`).
* `inject.script` (`init`: before every document; `now`: once), `inject.style` (web), `inject.eval` (returns JSON).
* `net.route` (Delay, Respond, PatchJson, Fail, ...), `net.unroute`, `net.shape` (CDP network conditions on web,
  bridge delays on mobile), `net.cassette` (enter starts record or replay of the session's traffic against the
  cassette store directory the hook gives, exit stops it). Mobile uses the bridge network shim.
* `fake.arm`, `fake.clear`, `mobile.*`: the bridge commands of the same name; `fake.arm` media is staged into the
  worker's media folder and served as a fixture.
* Mobile routing in the port: `capture.shot`, `capture.record`, `capture.hold` and the checkpoint shot are taken by
  the hub through the device pool (`DeviceCapture`), with the worker preparing the screen (`_capture.prepare` idles
  and hides targets, `_capture.restore`, `_recording` start/stop for the step log). `ui.swipe`, `ui.drag`,
  `ui.deeplink`, `ui.open_url` and any op with a native, `point` or `rect` target run on the native surface.
* The bridge resolves targets strictly (`match`, `inspect`): exact text by default (case and accents significant),
  `exact=false` folds case and accents, `within` scopes to the subtree, `near` sorts by box distance, nested matches
  collapse to the outermost element; errors carry `code` (`target_not_found`, `target_ambiguous`).
* Bridge presses and toggles wait at most 1 s for an async handler (a handler waiting on a system prompt would
  otherwise block the tap); handler errors are emitted as `bridge.handler_error` events. `inject.eval` bodies are
  async, so `await` works on mobile.
* The bridge network shim wraps both `XMLHttpRequest` and `globalThis.fetch` (Expo replaces `fetch` with a native
  implementation that bypasses XHR); blob responses are supported.
* Overlay icons come from one set (`IconName` in `ir/ops/overlay.py`, glyphs in `workers/kit/src/icons.mjs`).
* `ui.state` on web reads `window.__ehxState(path)`; without it the op is `op_unsupported`.
* `session.as` runs the app runtime's login (section 7); without one it is `login_unsupported`.

## 7. App runtimes

```python
@eks.app("web-app")
class WebApp:
    def prepare(self, ctx: eks.AppContext) -> eks.AppRuntime: ...   # start dev servers, return url and options
    def boot(self, ctx: eks.AppContext, worker: eks.WorkerHandle) -> None: ...   # install, launch, deep link
    def login(self, ctx: eks.AppContext, worker: eks.WorkerHandle, persona: eks.PersonaLogin) -> None: ...
```

* `AppContext`: `app` (profile), `platform`, `tree`, `lease` (sid, resource, device), `backend` (`BackendInfo`
  or `None`), `run`, `spawn(argv, cwd=, env=, ready=)` (starts a process registered in the procs registry with this
  run as owner; returns a handle with `url`, `port`, `stop()`), `log`.
* `AppRuntime`: `url`, `api_url`, `options` (merged into the worker config), `env`.
* Without an `@eks.app` for the profile, the runtime is the profile's `[web] url` / `[mobile]` fields.
* `[web] browser = "launch"` launches a local Chromium (with a window and a mock keychain) instead of the leased
  profile; tests use it. Nothing the harness starts runs headless: pool Chrome refuses `--headless` in
  `browser.extraArgs`, emulators start without `-no-window`, and a simulator lease fails when Simulator.app or
  DeviceHub.app cannot show its window.
* `AppContext.device` is the leased device record (udid, serial) and `AppContext.native` its native surface, so
  runtimes can install apps and clear stale system prompts (`ctx.native.dismiss_alerts()`).
* `[mobile] bridge = false` marks a native app without the harness bridge: every UI op runs on the native surface,
  `check.checkpoint` checks `page_errors` (logcat crash buffer) and `route` (resumed activity) natively, captures
  skip the bridge, and bridge-only ops are `op_unsupported`.
* Runtime `options` merge into the top level of the worker config (`options: {...}` merges into its options).
* Driver ports, and with them the app runtime's prepare and boot, open while the run is preparing, so app start-up
  never counts against the first node's timeout; a port that cannot open ends the run with `driver_error`.
* `[mobile] ios_bundle` / `android_package` name the app for the permission policy; the project settings
  `apps.iosBundleIds` / `apps.androidPackages` are the fallback.
* Dev servers started with `ctx.spawn` are keyed by tree and backend URL and reused across runs while their owner
  process (the hub) lives; they stop with the hub or when the reaper finds their owner dead.

## 8. Native surface (BL-011)

* Android: `uiautomator dump` for the tree, element bounds for targets, `adb shell input tap|swipe|text|keyevent`
  for actions. `adb input text` types ASCII only; other text on the native surface is `native_unavailable`.
* iOS: `idb ui describe-all` and `idb ui tap|swipe|text` when idb is installed, else `native_unavailable`.
  Transient "no translation object" answers are retried, a stale companion (HID connection invalidated) is restarted
  once with `idb kill`, and `ui.deeplink`/`ui.open_url` confirm iOS's "Open in <app>?" prompt.
* Supported on the native surface: `ui.tap`, `ui.long_press`, `ui.fill`, `ui.type`, `ui.swipe`, `ui.exists`,
  `ui.visible`, `ui.text`, `ui.wait_for`, `ui.wait_gone`, `ui.tree`, `expect.target`; others are
  `target_unsupported`.
* Permission policy: `SessionSpec.permissions` (`"ask"` default, `"grant"`, `"deny"`); the runtime grants or revokes
  the app's runtime permissions before launch (`adb shell pm grant|revoke`, `xcrun simctl privacy`).
* Authoring: `by.text("İzin ver").native` (a `Target` with `surface="native"`), usable wherever a target is; the
  validator rejects native targets on web. `@eks.flow(permissions=)` and `sc.take(permissions=)` set the policy.

## 9. Process ownership (BL-017)

* Workers and dev servers start with `start_new_session=True`; the owner kills the process group in `stop`, `atexit`
  and its SIGTERM/SIGINT handlers.
* Workers get `EHX_OWNER_PID` and `EHX_IDLE_EXIT_MS`; `lifetime.mjs` exits when stdin closes (the owner keeps a pipe
  open), when `process.ppid` is 1 or the owner pid is gone, and after the idle time without a request.
* Registry: `<state>/procs/<id>.json` `{id, kind, pid, pgid, startedAt, owner: {pid, startedAt}, tree, install,
  argv}`; start times come from `ps -o lstart` (macOS) or `/proc/<pid>/stat` (Linux).
* `reap(registry)`: for each entry whose owner pid is dead or has another start time, kill the group and remove the
  entry; entries whose process is gone are removed. The hub runs it at startup and on its housekeeping timer.
* `eks-harness reap [--all-installs] [--yes] [--json]`: lists registry orphans and, with `--all-installs`, processes
  with PPID 1 whose command line matches `eks_harness/_workers/*/src/server.mjs` or a harness dev server; kills only
  after confirmation (`--yes` without a terminal). `doctor --kill-orphans` is the same.

## 10. Tests

* `tests/drivers/test_wire.py`: wire form of every worker op, schemas, `ops.d.ts` up to date.
* `workers/kit/test/*.test.mjs` (node --test): op loop, schema validator, targets.
* `tests/drivers/test_port.py`: port against a stub worker (HTTP server) and against the real web worker with a
  headless Chromium on a local page (skipped without Playwright's browser).
* `tests/drivers/test_procs.py`: registry, reaping, and the SIGKILL test (a spawned owner with a worker child is
  killed with -9; the worker exits within its parent-watch interval).
* Engine end to end: a flow over a local test page run through `POST /api/runs` with the real web worker.
* Mobile ops are tested against a stub bridge; real-device runs are the user-approved verification step.
