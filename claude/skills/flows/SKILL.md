---
name: flows
description: "Write and run Python flows that drive a leased browser, simulator or emulator through a whole scenario in one command: navigation, input, checkpoints (URL or route, layout spill, errors, failed requests), screenshots and annotated recordings with contact sheets. Use for verifying UI changes and for demo videos."
---

# Flows

A flow is one Python file with `def flow(app)` in `<app>/.harness/flows/` (`<feature>-check.py`,
`<feature>-demo.py`). One command runs it end to end and prints a report.

```bash
eks-harness flow run src/web-app/.harness/flows/login-check.py
eks-harness flow run flows/login-check.py --platform ios --set user=admin
eks-harness flow run -e "app.goto('/settings'); result = app.text('h1')" --platform web
eks-harness flow list
```

MCP: `flow_run(flow=..., platform=..., params=..., backend=...)` returns the same report as JSON.

Backends: `--backend <backend lease id | backend id | definition>` (or `[backend]` in `app.toml`) links the
flow's lease to a shared backend for the lease's lifetime; the run waits until it is ready, `app.backend`
holds its info (`url`, `urls`, `credentials`, `env`, `commit`, `branch`) and `app.toml` can use
`${EKS_BACKEND_URL}`. In `app.py`, `self.link_backend("api", tree=...)` does the same from `prepare()`.

## App setup

`<app>/.harness/app.toml` names the project, its `tags` (put on every capture, for example `["web-app"]`),
platforms, URLs, Metro port, fixtures and checks. A `.harness/`
outside the app drives it with `[app] extends = "../path/to/app"` (profile and `App` inherited).
`<app>/.harness/app.py` may define `class App(WebApp)` or `class App(MobileApp)` with app helpers
(login screens, menus) and `boot()` that starts the app before the worker attaches. Extend those
helpers instead of repeating steps in flows. Plugins can add helpers with `flow_helpers`.

## The app object

Web: `goto(path)`, `nav(path)`, `click(t)`, `fill(t, v)`, `type(t, v)`, `press(key, t=None)`,
`select`, `check`, `choose(trigger, option)`, `hover`, `wheel`, `wait_for(t)`, `wait_gone(t)`,
`wait_url(pattern)`, `settle()`, `text(t=None)`, `value`, `count`, `exists`, `visible`, `tree(t=None)`,
`js(fn_source, arg)`, `api(method, path, body)`, `login(...)` (from the repo plugin),
`shot(name, phone=, full=, focus=)`.

Mobile: `press(t, long=)`, `fill(t, text, submit=)`, `type`, `submit`, `toggle`, `scroll(t, to=)`,
`navigate(name, params)`, `go_back()`, `route()`, `tree()`, `text`, `exists`, `wait_for`, `idle()`,
`state(path)`, `dispatch(action)`, `js(code)`, `arm(slot, **spec)` (fakes), `patch`, `inject`,
`shot(name)`.

Both: `checkpoint(label)` (checks plus a screenshot), `note(text)`, `emit(name, data)`,
`title`, `caption`, `callout`, `highlight`, `spotlight` (drawn live into the app), and recordings:

```python
def flow(app):
    with app.recording_of("settings-demo", title="Settings", subtitle="Theme switch"):
        app.goto("/settings")
        app.caption("Pick a theme", step=1)
        app.callout("role=radio:Dark", "Dark mode")
        app.click("role=radio:Dark")
    app.checkpoint("dark theme")
```

Targets: `#testId`, `text=...`, `role=button:Save`, `label=...`, `placeholder=...`, `css=...`, plain
text, or `{"role": "button", "name": "Save", "nth": 1}`.

## Working rules

1. One flow per question; rerun the whole flow after a fix (it takes seconds).
2. Read every screenshot and every contact sheet the report prints before you conclude anything. For timing
   questions (did the toast appear by 2 s, does the cut land on the beat) run `eks-harness video check` on the
   recording (skill `video-review`).
3. Check the checkpoint lines: URL or route, layout spill, console errors, failed requests, alerts.
4. Keep driver leases short: release the lease when the run is done (`--release` on the run, or
   `eks-harness lease release`). A linked backend keeps running for the next run until its idle timeout.
5. Before/after captures start with "before". When a task asks for a before/after video or screenshots,
   record the before take first, on the untouched code, before changing anything: write the
   `<feature>-demo.py` flow with a `take=before|after` param, run `take=before`, check it, then edit code.
   Both takes use the same persona, page, filters and data. Recreating the old code afterwards needs an extra
   worktree or overwriting work in progress.
