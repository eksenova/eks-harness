# Apps: profiles, runtimes, devices

## app.toml

`<app>/.harness/app.toml` is the app profile. A flow names it with `@eks.flow(app="<name>")`.

```toml
[app]
name = "reference-web"
platforms = ["web"]

[web]
url = "http://127.0.0.1:5391"     # required for web; the runtime may override it
api_url = "http://127.0.0.1:8080"
home = "/"
nav = "location"                  # how ui.nav navigates: location, next, hook
locale = "tr-TR"
timezone = "Europe/Istanbul"
browser = "lease"                 # lease (pool Chrome profile) or launch (a local Chromium with a window)
hide = ["[data-testid='clock']"]  # hidden in captures
network_ignore = ["/sockjs-node"] # requests the failed_requests check ignores
viewports = { kiosk = { width = 1280, height = 1024, scale = 1.0, mobile = false, touch = true } }

[mobile]
metro_port = 8181
ios_bundle = "com.example.app"     # for the permission policy and cleanup
android_package = "com.example.app"
bridge = true                      # false: a native app without the harness bridge
fixtures = ["fixtures"]            # folders fake.arm(fixture=...) reads
locale = "tr-TR"
timezone = "Europe/Istanbul"

[defaults]                         # session defaults every flow of the app starts from
timeout = "10s"
viewport = "desktop"
pace = "demo"
noise = ["ResizeObserver loop"]
checks = ["console_errors", { kind = "route", params = { expected = "/" } }]

[checks]
noise = ["Download the React DevTools"]

[backend]                          # optional: the backend flows of this app use
definition = "eksenapi"
```

Flow options (`viewport=`, `timeout=`, `checks=`, `noise=`, ...) override the profile; `noise` and
`checks` are merged.

## App runtimes (`@eks.app`)

Without a runtime, the profile's `[web] url` and `[mobile]` fields are used as they are. A runtime in a
trusted repo plugin prepares the app for each run. The hub calls `prepare` and `boot` while the run is
preparing, so start-up never counts against the first node's timeout.

```python
import eks
from eks_harness.drivers.runtime import AppContext, AppRuntime, AppRuntimeError, PersonaLogin, RuntimeWorker


@eks.app("reference-web", description="Vite and React reference app served by its dev server")
class ReferenceWeb:
    def prepare(self, ctx: AppContext) -> AppRuntime:
        url = "http://127.0.0.1:5391"
        ctx.spawn(["npx", "vite", "--host", "127.0.0.1", "--port", "5391", "--strictPort"],
                  key="reference-web-5391", cwd=ctx.root / "web-app", ready_url=f"{url}/", timeout=120)
        return AppRuntime(url=url)

    def login(self, ctx: AppContext, worker: RuntimeWorker, login: PersonaLogin) -> None:
        persona = ctx.persona(login.persona)
        worker.call("inject.eval", {"source": "(t) => localStorage.setItem('token', t)",
                                    "arg": persona["token"]})
```

- Hooks: `prepare(ctx) -> AppRuntime` (dev servers, install, return `url`, `api_url`, `options`, `env`),
  `boot(ctx, worker)` (launch, deep link, wait for the bridge), `login(ctx, worker, login)` (what `as_=` and
  `s.as_()` call; without it they fail with `login_unsupported`), `close(ctx)`.
- `AppContext`: `app`, `platform`, `tree`, `root`, `profile`, `lease`, `session`, `run`, `backend`,
  `backend_url`, `backend_info`, `persona(key)` (seed or backend persona credentials), `device` (udid or
  serial), `native` (the native surface; `ctx.native.dismiss_alerts()` clears stale system prompts), `log`,
  `spawn(argv, key=, cwd=, env=, ready_url=, ready_log=, timeout=)`.
- `ctx.spawn` registers the process with the hub's process registry: it is keyed by tree and backend URL,
  reused across runs while the hub lives, and killed with its owner. Nothing the harness starts outlives it
  (`eks-harness reap` lists and removes orphans).
- `worker.call(op, args)` runs a worker op directly, for example `mobile.idle` while waiting for the bridge.
- The reference runtimes for web (Vite), native Android and Expo iOS are in
  `examples/reference/.harness/plugins/reference/reference_apps.py`.

## Web

- The web worker drives a leased profile of the hub's shared, windowed Chrome over CDP with Playwright.
  `browser = "launch"` starts a local Chromium instead (tests use it). Nothing runs headless.
- Input is synthetic DOM events by default; `real=True` on an action gives trusted Playwright input.
- `settle` waits for hydration, network quiet and DOM quiet; `settle(strict=False)` returns instead of
  failing on a page that never stops changing.

## iOS and Android

- Simulators and emulators come from the hub's pools (`eks-harness devices list`, skill
  `eks-harness:devices`). The run leases one and releases it at the end.
- React Native apps embed `@eks-harness/react-native` (dev builds only):

  ```tsx
  import { HarnessRoot, startHarnessBridge, registerFake } from "@eks-harness/react-native";

  startHarnessBridge({ navigation: { app: navigationRef }, store, logout: performLogout,
                       alert: showAlert, closeDevMenu, components: { PromoBanner } });
  export default () => <HarnessRoot><App /></HarnessRoot>;
  ```

  Enable it with `EXPO_PUBLIC_EHX_HARNESS=1` and point it at the worker with `EXPO_PUBLIC_EHX_HARNESS_URL`
  (the runtime's `boot` passes `worker.url`). The bridge resolves targets strictly (exact text by default,
  `exact=False` folds case and accents), serves `s.mobile.*`, live overlays, fakes and the network shim.
- Fakes: the app registers slots (`registerFake("nfc", handler)`, the NFC helper, and the Expo image and
  document picker helpers in `@eks-harness/react-native/expo`); flows arm them with `s.fake.arm(...)`.
- `[mobile] bridge = false` marks a native app: every UI op runs on the native surface (Android
  uiautomator, iOS idb), `check.checkpoint` checks `page_errors` (crash log) and `route` (resumed activity),
  and bridge-only ops answer `op_unsupported`.
- Screenshots and recordings are taken by the hub through the device pool (`simctl io`, `adb
  screenrecord`), re-encoded to a constant frame rate and aligned with the step log.
- iOS 27 needs the UIScene life cycle; an Expo template that lacks it needs a config plugin (see
  `examples/reference/ios-app/plugins/with-scene-lifecycle.js`).
