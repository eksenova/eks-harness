---
name: eks-flows
description: "Write and run @eks.flow plans that drive a web app in a leased Chrome profile, or an iOS or Android app on a pooled simulator or emulator: the Session surface (s.ui, s.web, s.mobile, s.api, s.fake, s.overlay), expect, checkpoints, screenshots, DOM and accessibility dumps, HAR, recordings with live overlays, annotated screenshots, native system dialogs and permissions, fakes, and app runtimes (@eks.app, app.toml). Use for verifying UI changes, reproducing bugs and recording demo videos."
---

# Flows

A flow is a function decorated with `@eks.flow` in `<app>/.harness/flows/` or `.harness/flows/`. One
command plans it, runs it on the hub (lease, backend and seed included) and prints a report.

```python
import eks
from eks import by, expect, t
from eks.checks import use


@eks.flow(app="web-app", seed="standard", as_="harman-admin", viewport="desktop", timeout=t("10s"))
def new_invoice(s: eks.Session):
    s.ui.nav("/tenant/invoice/new")
    s.ui.fill(by.label("Müşteri"), "Moda Şube")
    s.ui.type("#amount", "125.50")
    s.ui.select("#currency", "USD")
    s.ui.tap(by.role("button", name="Kaydet"))
    expect.eventually(by.test_id("toast")).text(contains="kaydedildi")
    s.checkpoint("saved", checks=["console_errors", "failed_requests", use("route", expected="/tenant/invoice/list")])
    s.shot("saved", full=True)
    return {"route": s.ui.route()}
```

```bash
eks-harness run src/web-app/.harness/flows/new_invoice.py
eks-harness run flows/scan.py --platform ios --set tenant=harman
eks-harness run flows/x.py --backend "$EKS_BACKEND_LEASE"    # share a backend lease you hold
eks-harness run flows/x.py --seed fresh                       # restore the seed even if the backend has it
eks-harness run flows/x.py --fake '*'                         # simulate every op, no devices
eks-harness run flows/x.py --json                             # the report as JSON
```

MCP: `eks_run(spec="src/web-app/.harness/flows/new_invoice.py", platform=..., params=..., backend=...)`
returns the same report. To try steps one at a time first, open a session (skill `eks-harness:eks-mcp`).

## Reading the report

The run prints one line per node (`<status> <op> <node> <src> <ms>`), then the report: status, failures with
source line and failure screenshots, a checkpoint table, `artifacts:` lines (`<kind> <name> <page url> <raw
url>`), `values`, timings and the session link. Exit 0 passed, 1 failed, 12 paused, 13 aborted.

Look at what it captured before you conclude anything:

- Screenshots: `eks-harness artifacts download <id> -o <dir>` and Read the file, or MCP
  `get_artifact(artifact_id=<id>, image="full")`.
- Recordings: `eks-harness video sheet <id>` and Read the local sheet, or MCP `video_sheet`; for timing
  questions run `eks-harness video check` (skill `eks-harness:video-review`).
- Checkpoint rows: route, layout spill, console and page errors, failed requests, error toasts.

## Session surface

| Group | Calls |
| --- | --- |
| navigation | `s.ui.nav(route, params=)`, `back()`, `reload()`, `open_url(url)`, `deeplink(url)` |
| input | `tap`, `double_tap`, `long_press(hold=)`, `hover`, `fill(target, value, submit=)`, `type(target, value, pace=)`, `clear`, `key(key, target=)`, `select(target, value)`, `check(target, on=)`, `choose(trigger, option)`, `scroll(target, to=, by=)`, `swipe(direction, target=)`, `drag(source, to)` |
| reads (Refs) | `text`, `value`, `attr` (web), `count`, `exists`, `visible`, `tree(max_lines=)`, `url()` (web), `route()`, `state(path)`, `layout` |
| waits | `wait_for(target, state=, timeout=)`, `wait_gone`, `wait_url(pattern)` (web), `settle(strict=)` |
| assertions | `expect(target).visible()/hidden()/enabled()/disabled()/checked()/text()/value()/count()/attr()`, `expect(ref).eq/ne/contains/matches/gt/ge/lt/le/truthy/falsy/length`, `expect.soft(...)`, `expect.eventually(...)` |
| evidence | `s.checkpoint(label, viewport=, checks=, full=, soft=)`, `s.shot(name, element=, focus=, full=, hide=)`, `s.dump("dom"\|"a11y"\|"mhtml")`, `s.har()` |
| recording | `with s.record(name, pace=, viewport=, pointer=, hide=):`, `s.hold(t)` |
| overlays | `s.overlay.title/caption/callout/spotlight/highlight/arrow/blur/redact/icon/image/step`, `s.overlay.remove(key)`, `s.overlay.clear()` |
| backend | `s.api.get/post/put/patch/delete(path, json=, query=, as_=, expect_status=)`, `s.as_(persona)`, `s.backend.info` |
| web | `s.web.viewport(name)`, `s.web.wheel(target, dx=, dy=)`, `s.web.logs("console"\|"errors"\|"network"\|"websocket")`, `s.web.clear_logs()` |
| mobile | `s.mobile.dispatch(action)`, `state(path)`, `patch(target, props=, style=, text=, key=)`, `unpatch(key)`, `mount(component, props=, key=)`, `unmount(key)`, `invoke(target, prop, *args)`, `toggle`, `submit`, `idle()`, `route_names()`, `logout()`, `close_dev_menu()`, `pointer(target)` |
| fakes | `s.fake.arm(slot, mode=, media=, fixture=, data=)`, `s.fake.clear(slot)` |
| network, injection | `s.net.*`, `s.inject.script/style`, `s.eval(js)` (skill `eks-harness:eks-network`) |
| debug | `s.debug.break_(label)`, `s.debug.pause_on_error()` (skill `eks-harness:eks-debugging`) |
| any op | `s.op("ui.tap", target="#save")`, `s.call(ArgsModel(...))`, plugin macros as `s.<namespace>.<name>(...)` |

Actions take `timeout=`, `settle=` (wait for network and DOM quiet after the action, default on) and
`real=` (trusted input on web, for file pickers and native behaviour). Full argument lists:
`references/session.md`, `docs/reference/ops.md`, or `eks-harness ops ui.tap`.

## Recordings and overlays

```python
with s.record("invoice-tour", pace="demo", viewport="phone", pointer=True):
    s.overlay.title("Yeni fatura", subtitle="Kontör işlemleri", hold=t("2s"))
    s.overlay.caption("Tutarı girin", position="bottom", step=1, hold=t("2s"))
    s.ui.fill("#amount", "2450.10")
    s.overlay.callout("#total", "Anlık kur", placement="top", hold=t("2s"))
    s.overlay.blur(by.test_id("customer-tckn"), radius=12, keep=True, key="tckn")
    s.ui.tap("#submit")
    s.hold(t("1s"))
    s.overlay.remove("tckn")

shot = s.shot("summary", full=True)
annotated = eks.annotate(shot, [eks.overlay.callout("#total", "Anlık kur"),
                                eks.overlay.redact(by.test_id("customer-iban"))], style="review")
```

- Overlays inside `s.record` are drawn live into the app and end up in the video; `hold=` keeps one for a
  while, `keep=True` until `s.overlay.remove(key)` or `clear()`.
- `eks.annotate(artifact, overlays, style=)` draws on a screenshot after the fact, anchored on the box map
  saved with the shot (`title` is not available on screenshots). For overlays on a recording after the fact,
  use an edit (skill `eks-harness:eks-edit`).
- `pace="demo"` slows input to a watchable speed; `s.hold(t)` only works inside a recording.
- Overlay icons: `check`, `cross`, `warning`, `info`, `tap`, `star`, `plus`, `minus`, `lock`, `bell`,
  `arrow-up`, `arrow-down`, `arrow-left`, `arrow-right`, `search`, `heart`.
- Demo videos skip the way in: log in with `as_=` (or before `s.record`), start the recording on the page
  the demo is about.

## Native dialogs and permissions (iOS, Android)

System permission prompts, OS sheets and in-app native alerts sit outside the React Native tree. Reach
them with `.native` targets:

```python
@eks.flow(app="mobile-app", permissions="ask")
def camera_permission(s: eks.Session):
    s.ui.tap(by.test_id("camera"))
    expect.eventually(by.text("While using the app").native, timeout=t("10s")).visible()
    s.shot("permission-prompt")
    s.ui.tap(by.text("While using the app").native)
```

- `permissions="grant"` or `"deny"` on `@eks.flow` sets the app's runtime permissions before launch
  (`pm grant/revoke`, `simctl privacy`); `"ask"` (default) leaves the prompts to the flow.
- Native ops: `tap`, `long_press`, `fill`, `type`, `swipe`, `exists`, `visible`, `text`, `wait_for`,
  `wait_gone`, `tree`, `expect`. Android types ASCII only on the native surface; iOS needs `idb`.
- `ui.deeplink` and `ui.open_url` confirm iOS's "Open in <app>?" prompt themselves.

## Apps

`<app>/.harness/app.toml` names the app and its platforms and holds its defaults; an `@eks.app` runtime
in a trusted plugin starts dev servers, installs and launches the app and logs personas in. See
`references/apps.md` (app.toml keys, runtimes, the React Native bridge, simulators and emulators).

## Working rules

1. One flow per question; rerun the whole flow after a fix (it takes seconds). Plan it first
   (`eks-harness plan`) to catch target and type errors without a device.
2. Read every screenshot and look at every recording before you report.
3. Strict targets are a feature: when a target is ambiguous, pick with `.within(...)`, `.near(...)` or
   `.nth(...)` instead of loosening it.
4. Never point a flow at a production backend; `eks-harness backend choose` shows what a branch gets.
5. When a feature needs data the seed lacks, extend the seed (skill `eks-harness:eks-data`).
