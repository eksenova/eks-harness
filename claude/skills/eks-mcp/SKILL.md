---
name: eks-mcp
description: "Agent workflow with the eks-harness MCP tools: one-shot ops (eks_do), interactive sessions (eks_session_open, eks_session_do, eks_session_close) and turning a session into a flow file (eks_session_export, eks_flow_save), running and controlling plans (eks_run, eks_plan, eks_validate, eks_run_status, eks_run_control), the op catalog (eks_ops), flows and seeds (eks_flows, eks_flow_get, eks_seeds, eks_seed_show), and the store tools for artifacts, shares and notes. Use when driving the harness from Claude rather than the CLI, or when exploring an app before writing a flow."
---

# MCP workflow

The tools call the hub; `tree` defaults to the project the session runs in. Every tool answers JSON; errors
carry issue codes and source lines.

## Pick the smallest tool that answers the question

| Need | Tool |
| --- | --- |
| One value or one action, no state kept (an API read, a page's URL, one screenshot) | `eks_do(op, args, platform=, app=, persona=, backend=)` |
| Several steps on the same screen while you work out a flow | `eks_session_open`, then `eks_session_do` per step |
| A flow you can rerun | `eks_session_export`, `eks_flow_save`, then `eks_run` |
| Run an existing flow, seed, edit or scene | `eks_run(spec="path/file.py::name")` |
| Check a plan without running it | `eks_plan(spec=...)` or `eks_validate(plan=...)` |
| What an op takes | `eks_ops(name="ui.tap")` (JSON Schema), `eks_ops(family="net")` |

## One-shot ops

```text
eks_do(op="api.get", args={"path": "/api/me"}, app="web-app", persona="harman-admin")
eks_do(op="ui.url", platform="web", app="web-app")
eks_do(op="capture.shot", args={"name": "home"}, platform="ios", app="mobile-app")
```

The hub plans a one-node run, acquires the lease and backend it needs, runs it and returns `{run, value,
artifacts, failure}`. Target fields take shorthand strings (`"#save"`, `"role=button:Kaydet"`).

## Sessions

```text
eks_session_open(app="web-app", platform="web", seed="standard", as_="harman-admin", viewport="desktop")
  -> {session: "<run id>", pause: {...}}
eks_session_do(session, op="ui.nav", args={"route": "/tenant/invoice/new"})
eks_session_do(session, op="ui.tree", args={"max_lines": 120})          # cheap look first
eks_session_do(session, op="ui.fill", args={"target": "#amount", "value": "2450.10"})
eks_session_do(session, op="capture.shot", args={"name": "filled"})    # returns the artifact
eks_session_export(session, name="new_invoice")                         # the steps as a .py flow, planned and validated
eks_session_close(session)                                              # ends the run, releases the leases
```

- A session is a paused debug run holding a lease (and a backend when you name `seed`, `as_` or `backend`).
  Each `eks_session_do` renews its timeout (30 minutes by default); close it as soon as you are done.
- Look cheaply before paying for an image: `ui.tree`, `ui.text`, `ui.url`, `ui.route`, `ui.exists`,
  `web.logs` cost a fraction of a screenshot and show empty states, permission errors and toasts. The
  plugin's guard refuses `capture.shot` right after an action until one of those ran.
- `eks_session_export` writes each op as its Python call (`ui.tap` -> `s.ui.tap(target)`); an op without a
  builder becomes `s.op("<op>", ...)`. Save it with `eks_flow_save(path="<app>/.harness/flows/x.py",
  source=...)`, which plans and validates every entry in the file, then run it with `eks_run`.

## Runs

```text
eks_run(spec=".harness/flows/x.py", platform="ios", params={"tenant": "harman"}, backend="bl_...", wait=120)
  -> {issues, report} when it ended, else {run, pause}
eks_run(source="import eks\n...", ...)                  # Python source planned as if it lived in .harness/<folder>/
eks_run(spec=..., breakpoints=["op:ui.tap"], debug=true)
eks_run_status(run_id, wait=60, report=true)            # wait for the end or the next pause
eks_run_control(run_id, action="do", op="ui.tree", args={})
eks_run_control(run_id, action="continue")
```

`fakes=["*"]` simulates every op without devices, useful for checking a plan's flow. The report has
`failures`, `checkpoints`, `artifacts` (each with `id`, `kind`, `name`, `url`, `rawUrl`), `values` and
`timings`.

## Flows and seeds

`eks_flows(kind="flow"|"seed"|"edit"|"scene"|"score")` lists entries with parameters, apps and platforms;
`eks_flow_get(path)`, `eks_flow_save(path, source, expect_sha256=)`, `eks_flow_delete(path)` (ask the user
first). `eks_seeds()` lists seed snapshots and `eks_seed_show(key)` shows one with its personas (no secrets).

## Store tools

`list_projects`, `list_sessions`, `list_artifacts(project=, session=, tag=)`, `get_artifact(artifact_id,
image="full")` (attaches the image so you can look at it), `share_artifact` (a `directUrl` that opens without
a login), `tag_artifacts`, `mark_seen`, `set_artifact_retention`, `search`, `add_note`, `session_timeline`,
`upload_artifact`, `lease_status`, and for recordings `video_sheet` (contact sheet images) and `video_check`.
Backends: `backend_acquire`, `backend_info`, `backend_renew`, `backend_release`, `backend_leases`,
`choose_backend`, `list_backends`, `list_backend_definitions` (skill `eks-harness:backends`).

Give the user the `url` (page) and `rawUrl` (file) of anything worth showing, with a line on what it shows.
