# M2 report: drivers

Contract: `contracts-m2.md`. Status: done. The exit check passed on 2026-10-14: the three reference flows pass on
real leases (shared Chrome, iOS simulator, Android emulator), including native dialogs, all with visible windows.

## What landed

| Area | Where | Notes |
| --- | --- | --- |
| Typed op protocol | `workers/kit/src/ops.mjs`, `schema.mjs`, `targets.mjs` | `/op`, `/snapshot`, `/health`; wire schema validation; typed errors; `resources` field for cassettes |
| Wire form | `drivers/wire.py` | Time to ms, presets to values, media to files, checkpoint check set resolved from the session |
| Port and provider | `drivers/port.py` | replaces the M1 fake path for non-simulated runs; `NodeFailure` from arg validation becomes `DriverError` |
| App runtimes | `drivers/runtime.py`, `eks.app` | `prepare`, `boot`, `login`, `close`; dev servers owned through the procs registry |
| Process ownership (BL-017) | `drivers/procs.py`, `kit/src/lifetime.mjs`, `cli/reap_cmds.py` | owner pid and start time, hub reaping, `eks-harness reap`, `doctor --kill-orphans` |
| Web ops | `workers/web-driver/src/ops.mjs` | every web worker op; strict targets (`within`, `near`, `nth`); checkpoints; recordings; HAR since last capture; overlays with all eleven kinds; `net.route/shape/cassette`; `inject.*` |
| Mobile ops | `workers/mobile-driver/src/ops.mjs` | every mobile worker op through the bridge; captures on the hub through the device pool |
| RN bridge | `sdk/react-native/src/bridge.tsx`, `overlay.tsx`, `network.ts` | strict `match`/`inspect` with coded errors; six new overlay kinds; XHR network shim (routes, shaping, cassettes) |
| Native surface (BL-011) | `drivers/native.py` | Android uiautomator + adb, iOS idb; strict targets; permission policy via `pm grant/revoke` and `simctl privacy` |
| Device captures | `drivers/devices.py` | screenshots and recordings through `ctx.pools.devices`, encoded with the store encoder |
| Cassette store | `drivers/cassettes.py` | `FileCassetteHook` registered on the hub |
| Lazy backend | `eks/tracing.py` | a session gets a backend only when it names seed, backend or persona or uses an op with the backend role |

## Fixes found on the way

* `workers_root()` preferred the gitignored wheel copy `src/eks_harness/_workers` (stale since Oct 11) over the
  source tree, so a dev checkout ran old worker code. The source tree now wins when it exists.
* Overlays were captured mid fade-in by an immediate screenshot; a kept overlay now waits for its fade.
* Overlay icon names were free text with no icon set; `IconName` is now a closed set shared with the kit glyphs.

## Tests

* Python: full suite 5,124 passed, 7 skipped (M1 ended at 5,098), including `tests/drivers/test_port.py` (stub worker; the real web worker on a
  headless Chromium against a local page with ops, strict matching, assertions, checkpoints, shots, cassette record
  and replay, route patching; mobile routing with a fake device pool), `test_native.py`, `test_wire.py`,
  `test_procs.py`.
* JS: kit 13, web-driver 4, mobile-driver 10 (op loop against a fake bridge), react-native SDK 22 (network shim).
* Typechecks: all four JS packages; pyright and ruff clean on the M2 files.

## Reference flows (exit check)

Apps and flows live in `examples/reference/` (plugin `reference` holds the three app runtimes). They ran on a hub
started from this worktree with its own home (`~/.eks-dsl-hub`, port 55764) and its own Chrome, simulator
("EksDsl iOS 1", iOS 27) and emulator (`eksdsl_harness_1`, from the base AVD `eksdsl_base`), so the real hub and
its devices were not touched.

| Flow | App | Covers | Last passing run |
| --- | --- | --- | --- |
| `web_reference.py` | Vite + React (`web-app`) | strict targets, fill/type/select/check, `ui.state`, assertions, dialog, `net.route` respond and unroute, checkpoint (all web checks), shot, a11y dump, HAR, recording with overlays, live overlays | `01M4XMF5N3HT06XMWC7MBAV9HM` |
| `android_reference.py` | native Kotlin app, no bridge (`android-app`) | everything on the native surface: taps, fill, assertions, in-app AlertDialog, the system camera permission dialog, deep link, native checkpoint (crash log, activity), recording, swipe | `01M4XMFKCW5YXJS5CCRP8V922G` |
| `ios_reference.py` | Expo 57 / React Native 0.86 with the bridge (`ios-app`) | bridge strict targets, fill, toggle, store state and dispatch, `net.route` on `fetch`, native in-app Alert, the system location prompt, `ui.nav`, deep link with the iOS open prompt, overlays, recording, checkpoint | `01M4XMND80PVBJ7TY9SZ66JJA7` |

Mid-run screen captures confirmed the Chrome window, the emulator window and the simulator window (DeviceHub) were
visible. Each flow also passed twice back to back.

### Found and fixed by the reference runs

* Admission refused every real run ("real drivers arrive in M2"); it now asks whether the built-in worker drivers can
  run (node and the worker sources) and keeps `driver.missing` for when they cannot.
* App boot ran inside the first node's timeout; ports now open while the run is preparing.
* Artifact names lost their extension; ingest keeps it.
* Bridge: text nodes ignored `testID`; presses awaited async handlers (a tap that opens a system prompt hung);
  `eval` could not `await`.
* Network shim: Expo's native `fetch` bypassed the XHR hook, and RN `fetch` uses blob responses.
* Native surface: iOS's "Open in <app>?" prompt for deep links, transient "no translation object" answers, stale idb
  companions, and system prompts that outlive a failed run (runtimes clear them with `ctx.native.dismiss_alerts()`).
* Simulator windows: the pool only warned when the window could not be shown; it now retries and fails the lease,
  and the worker's local launch mode is headed with a mock keychain.
* iOS 27 requires the UIScene life cycle; Expo 57's template does not adopt it, so the reference app carries a
  config plugin (`plugins/with-scene-lifecycle.js`) doing what EksenGOLD's committed `ios/` does by hand.

## Known limits

* Android `adb input text` types ASCII only; non-ASCII text on the native surface is refused with a clear error
  (app-surface typing handles Turkish text).
* Native Android lookups cost one `uiautomator dump` each (about 1 s), so native-only flows are slower.

## Left for later milestones

* `check.explore` (M6) and `score.take` (M5) answer `op_unsupported`.
* `visual` and `perf` checks report `check_unsupported` until M6.
