# Drivers, workers and flows

## Pieces

| Piece | Where | Role |
| --- | --- | --- |
| Worker kit | `workers/kit` | daemon client, script runner, DevTools link, config, extension loader |
| Web driver worker | `workers/web-driver/src/server.mjs` | drives one leased Chrome profile over CDP with Playwright |
| Mobile driver worker | `workers/mobile-driver/src/server.mjs` | drives React Native apps through Metro's DevTools and the in-app bridge |
| RN bridge | `sdk/react-native` (`@eks-harness/react-native`) | runs inside dev builds; tree, targets, actions, overlays, patches, fakes, events |
| Worker manager | `eks_harness.drivers.workers` | one worker per lease sid, spawned on a free port, reused while code and config match |
| Driver sessions | `eks_harness.drivers.sessions` | `act` / `observe` / `capture` / `events` over a worker, used by scores and MCP |
| Flows SDK | `eks_harness.flows` | `App`, `WebApp`, `MobileApp`, `run_flow`, `eks-harness flow run` |
| Encoder | `python -m eks_harness.capture.encode` | timed frames or a recording to the verified deliverable MP4, step-log trim |

## Worker configuration

The manager writes `config.json` (mode 600) into `<state>/workers/<kind>-<sid>/` and starts
`node server.mjs` with `EHX_WORKER_CONFIG` pointing at it. The worker binds `127.0.0.1:0`, writes
`worker.json` (`{ready, port, pid, ...}`) and heartbeats its lease sids every 20 s.

Web keys: `baseUrl`, `sid`, `home`, `locale`, `timezone`, `nav` (`location`, `next`, `hook` for
`window.__ehxNavigate`), `apiBaseUrl` (`apiFromBackend` when it came from the linked backend),
`backend` (the linked backend's info with secrets: `id`, `lease`, `generation`, `url`, `urls`,
`credentials`, `env`, ...; it does not count toward the worker's config hash), `viewports.{desktop,mobile}`, `hideSelectors` (the project's
`capture.hideSelectors` setting plus app.toml `[web] hide`),
`networkIgnore`, `wsIgnore` (regex strings), `appOrigins`, `theme`, `pace`, `encoder`,
`playwrightFrom`, `extensions`, `options` (`settleReact`, `heartbeatMs`, `bypassCsp`, `insecureTls`, ...).

Mobile keys: `sids` (`{ios, android}`), `metroPort`, `fixtureDirs`, `fakeSlots`, `pace`,
`extensions`, `backend`, `options`.

Heartbeat answers (`POST /api/leases/<sid>/heartbeat`) carry the lease's backend link (`id`, `lease`,
`status`, `generation`, `url`). When the generation or backend changes the worker reads the full info
(`GET /api/leases/<sid>/backend?secrets=true`), calls `onBackendChange`, emits `backend.changed` on
`GET /events` and, on web, reloads the open pages (`options.reloadOnBackendChange`, default on).

## HTTP protocol

Web (all POST): `/status /goto /click /hover /wheel /fill /press /wait-for /eval /text /shot
/video/start /video/stop /dump /har/start /har/stop /console /logs /logs/clear /down`, runner
routes `/run /continue /run-wait /abort /run-status /exec /tree`, `GET /events` (SSE).

Mobile: runner routes as above, `GET /events` (SSE), app routes `GET /state`,
`GET /media-base64/<name>`, `POST /app-events`, agent routes `POST /arm /clear /reset`,
`GET /health /devtools`, WebSocket relay `/devtools/<platform>`.

Bridge: the worker evaluates `globalThis.__ehxBridge.dispatch('<{id, cmd, args}>')`; the app answers
through the CDP binding `__ehxReply` with `{type: result|hello|event, ...}`. Commands: `ping boot
tree snapshot find exists text press fill submit toggle invoke scroll navigate goBack route
routeNames annotate pointer logout closeDevMenu state dispatch layout idle waitFor eval patch
unpatch inject emit fakes`.

## Extensions

A plugin contributes `driver_extension` entries:

```toml
[[contributes.driver_extension]]
path = "web/auth.mjs"
platform = "web"          # or platforms = ["ios", "android"]
```

The module exports `export default function extend(kit) { return { ... } }`. `kit` carries
`config`, `settings` (the plugin's resolved settings), `id`, `log`, `daemon`, `emit`, `backend()` (the
linked backend's current info, credentials included, or null; mobile takes an optional sid), and per
worker: web `activePage ensureBrowser ensureMobile settle gotoPath locate present resolveTarget
readPageStorage writePageStorage syncContexts persist restore upload helpers handlers`; mobile
`bridgeCall pacedBridge fillInput laneFor armFake clearFakes captureDevice stateDir`.

Returned hooks: `helpers` (object, or `(lane) => object` on mobile; merged into script helpers),
`routes` (`{'/path': async (body) => result}`), `onContext(context)`, `onRestore(state, page)`,
`onRecordingStart(page)`, `status()`, `apiHeaders()` (used by the web `api()` helper),
`onEvent(event)` (mobile), `onBackendChange(next, previous)` (the linked backend was replaced or
re-linked; log in again with `next.credentials`).

`flow_helpers` contributions name a Python class mixed into the flow `App` for a platform.

## App profiles

`<app>/.harness/app.toml`:

```toml
[app]
project = "acme/web-app"
platform = "web"              # or platforms = ["ios", "android"]

[web]
url = "http://localhost:${WEB_PORT:-3000}"
api_url = "${API_URL}"
locale = "en-US"
nav = "location"
hide = [".devtools"]

[mobile]
metro_port = 8081
fixtures = ["fixtures"]
fake_slots = ["nfc", "camera"]

[checks]
noise = "analytics|sentry"
alerts = "[role=alert]"
```

`<app>/.harness/app.py` may define `App(WebApp)` or `App(MobileApp)` with app helpers, a
`prepare()` that runs after the lease and before the worker starts (backend, ports, dev server;
it may change `self.profile`), `worker_options()` merged into the worker config, and a `boot()`
that runs once the worker is attached (`self.worker.base_url` is known: Metro, install, launch,
login).

## Backends in flows

A flow's lease can hold a backend for its lifetime. The runner links one from `flow run --backend <bl_... |
backend id | definition> [--backend-tree] [--backend-key] [--backend-param K=V]` or from `app.toml`:

```toml
[backend]
definition = "api"          # or link = "<backend id>"; "none" links nothing
tree = "${API_TREE}"        # the tree it runs from (default: the app's tree)
key = "demo"                # optional identity instead of the tree
params = { tenant = "acme" }
```

The driver lease waits until the backend is ready. Before `prepare()` the app has `self.backend`
(`BackendInfo`: `url`, `urls`, `ports`, `credentials`, `env`, `commit`, `branch`, `version`, `worktree`,
`extra`) and `self.backend_link` (the lease, the backend record and the info), and `app.toml` is expanded
again with `EKS_BACKEND_URL`, `EKS_BACKEND_URL_<NAME>`, `EKS_BACKEND_ID`, `EKS_BACKEND_LEASE` and the
backend's env (`api_url = "${EKS_BACKEND_URL}"`). An `app.py` that picks its backend itself (a backend
policy) calls `self.link_backend("api", tree=..., params=...)` first thing in `prepare()`; it returns the
`BackendInfo` once the backend runs. Start app dev servers keyed by the app tree and the backend URL, not
by the harness instance, so later driver leases on the same backend reuse them.

A `.harness/` outside the app (an ad workspace, a demo folder) drives an existing app with
`[app] extends = "<path to the app, relative to the folder holding .harness>"`: the profile and
the `App` class come from that app's `.harness/`, the root is that app, and the local `app.toml`
tables override the inherited ones key by key (a local `app.py` replaces the inherited class).
