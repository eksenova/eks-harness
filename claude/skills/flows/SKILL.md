---
name: flows
description: Write and run Python flows that drive a leased browser, simulator or emulator through a whole scenario in one command: navigation, input, checkpoints (URL or route, layout spill, errors, failed requests), screenshots and annotated recordings with contact sheets. Use for verifying UI changes and for demo videos.
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

MCP: `flow_run(flow=..., platform=..., params=...)` returns the same report as JSON.

## App setup

`<app>/.harness/app.toml` names the project, platforms, URLs, Metro port, fixtures and checks. A `.harness/`
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
2. Read every screenshot and every contact sheet the report prints before you conclude anything.
3. Check the checkpoint lines: URL or route, layout spill, console errors, failed requests, alerts.
4. Release the lease when the work is done: `eks-harness lease release` (or `--release` on the run).
