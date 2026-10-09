---
name: eks-harness
description: Overview of eks-harness and when to use which part: the hub daemon, leases, devices and browsers, flows, evidence store, backends, nodes, video engine, scores, plugins, MCP tools and CLI. Load it first when a task involves running, verifying, capturing, recording or rendering anything with eks-harness, or when unsure which harness skill applies.
---

# eks-harness

eks-harness is one hub (a daemon, `eks-harness daemon`) plus optional nodes. The hub lends out
browser profiles, iOS simulators, Android emulators and backends as **leases**, drives them through
driver workers, stores every capture as an **artifact** under a project and session, runs **jobs** on
nodes, and renders **videos** and **scores** (one clock for edits, Blender, web scenes and device takes).

## Pick the right tool

| You want to | Use | Skill |
| --- | --- | --- |
| Check a UI change in the running app | a flow: `eks-harness flow run <app>/.harness/flows/x-check.py` | `eks-harness:flows` |
| Drive a device or page step by step | MCP `driver_act` / `driver_observe` / `driver_capture` | `eks-harness:web`, `:ios`, `:android` |
| Control a React Native app's state or UI live | RN bridge commands (`patch`, `inject`, `dispatch`, `arm`) | `eks-harness:react-native` |
| Start the app's backend, seed it, log in as a persona | `eks-harness backend ensure`, `backend seed`, `backend personas` | `eks-harness:backends` |
| Hand evidence to a PR or a person | `eks-harness share create`, artifact links | `eks-harness:evidence` |
| Annotate a screenshot | `eks-harness annotate` (images only) | `eks-harness:annotation` |
| Edit or render a video | `eks-harness video render`, MCP `video_*` | `eks-harness:video-editing` |
| See what renders now and what waits (video renders and recording encodes share one lane, one at a time; driver sessions never wait) | `eks-harness queue`, MCP `render_queue` | - |
| Review a video (one-image contact sheet, timeline checks) | `eks-harness video sheet`, `eks-harness video check`, MCP `video_sheet`, `video_check` | `eks-harness:video-review` |
| Sync Blender, web scenes and device takes to a song | `eks-harness score plan`, `score render`, live mode in the UI | `eks-harness:scores` |
| Write a Blender or HTML scene | `eks_harness.blender`, `@eks-harness/scene` | `eks-harness:blender-scenes`, `:web-scenes` |
| Render on GPUs elsewhere | nodes and jobs | `eks-harness:nodes` |
| Add an integration (backend, login, effect, driver, UI) | plugins | `eks-harness:plugin-development` |
| Something is broken | `eks-harness doctor`, logs | `eks-harness:troubleshooting` |

## Essentials

- `eks-harness` and `ehx` are the same CLI. Every command talks to the hub over HTTP; `--json` prints
  plain JSON. Exit codes: 0 ok, 3 hub down, 4 not logged in, 6 not found, 7 lease gone.
- A repo opts in with `.harness/project.toml` (project id, plugins, scores, Claude hook settings) and
  per-app `.harness/app.toml`, `.harness/app.py` and `.harness/flows/`.
- Repo plugins live in `.harness/plugins/<name>/harness-plugin.toml`; they load after approval
  (`eks-harness plugin trust <id>`) or when `project.toml` pre-trusts them.
- One harness instance per Claude session (`CLAUDE_CODE_SESSION_ID`); leases are released after their
  idle grace, or explicitly with `eks-harness lease release`.
- Never start a harness the user did not ask for; when a UI change stays unverified, say so.
- Every screenshot and contact sheet you produce must be opened with Read before you report; the
  plugin's Stop hook enforces it.
