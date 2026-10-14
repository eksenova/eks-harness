---
name: eks-harness
description: "Overview of eks-harness and its one Python API (`import eks`): the plan model, the hub, leases, backends, the evidence store, the CLI (`eks-harness run`, `plan`, `ops`, `do`, `runs`, `debug`, `seed`) and the eks_* MCP tools, with routing to the other eks-harness skills. Load it first when a task involves running, verifying, capturing, recording, seeding or rendering anything with eks-harness, or when unsure which harness skill applies."
---

# eks-harness

eks-harness is one hub (a daemon, `eks-harness daemon`) plus optional render nodes. You describe work in
Python with `import eks`; the code builds a typed **plan**; the hub validates it, acquires what it needs
(a browser profile, an iOS simulator, an Android emulator, a backend, a seed snapshot, a render slot), runs
it, journals every node and stores every capture as an **artifact** under a project and session.

## The plan model in one page

```python
import eks
from eks import by, expect, t


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def branches(s: eks.Session):
    rows = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
    s.ui.nav("/tenant/settings/branches")
    expect(by.text("Kadıköy")).visible()
    s.checkpoint("list")
    s.ui.nav(f"/tenant/invoice/list?branchId={rows.items.find(name='Kadıköy').id}")
    expect(by.test_id("invoice-row")).count(at_least=1)
    return {"branches": rows}
```

- Calling a builder (`s.ui.nav`, `s.api.get`, `expect(...)`) records one **node**; nothing runs while the
  function is traced. Values come back as lazy `Ref`s (`rows.items.find(name=...).id`), so the plan can
  pass a value from one node to another without running anything.
- Every duration is a `Time` (`t("2s")`, `t("300ms")`, `t("beat:8")`); bare numbers are rejected.
- Every element is a `Target` (`by.test_id("save")`, `"#save"`, `by.role("button", name="Kaydet")`), matched
  strictly: zero matches waits, two or more fail unless you pick one with `.nth()`, `.first` or `.last`.
- Assertions fail the run by default; `expect.soft` records without failing.
- Entry kinds: `@eks.flow` (drive an app), `@eks.seed` (data for a backend), `@eks.edit` (a video
  timeline), `@eks.scene` (gfx, Blender or web scene), `@eks.score` (one clock for all of it). Files live in
  `.harness/{flows,seeds,edits,scenes,scores}/` and `<app>/.harness/flows/`.

```bash
eks-harness plan .harness/flows/branches.py           # build and validate, no hub needed
eks-harness run .harness/flows/branches.py            # run on the hub, follow it, print the report
eks-harness run flows.py::branches --platform ios --set tenant=harman
eks-harness ops ui.tap                                # an op's arguments as JSON Schema
eks-harness do ui.url --platform web --app web-app    # one op, no file
eks-harness runs list | runs show <id> | debug <id>
eks-harness seed snapshots | seed show <key>
```

MCP: `eks_run`, `eks_plan`, `eks_validate`, `eks_ops`, `eks_do`, `eks_session_open/do/close/export`,
`eks_run_status`, `eks_run_control`, `eks_flows`, `eks_flow_get/save/delete`, `eks_seeds`, `eks_seed_show`, plus
the store tools (`list_artifacts`, `get_artifact`, `share_artifact`, ...).

## Pick the right skill

| You want to | Skill |
| --- | --- |
| Understand plans, Refs, Time, Targets, control flow and determinism | `eks-harness:eks-dsl` |
| Check a UI change in a running web, iOS or Android app, record a demo | `eks-harness:eks-flows` |
| Seed a backend, define resource kinds, log in as a seed persona | `eks-harness:eks-data` |
| Run, share and release backends (leases, info, logs, policies) | `eks-harness:backends` |
| Mock, delay or record network traffic, inject scripts or styles | `eks-harness:eks-network` |
| Pause a run, step through it, run an op in the paused session, compare repeats | `eks-harness:eks-debugging` |
| Checkpoint checks, media checks, review checks derived from an edit | `eks-harness:eks-checks` |
| Edit video: clips, effects, transitions, overlays, render settings | `eks-harness:eks-edit` |
| 2D motion graphics and the audio mix | `eks-harness:eks-gfx-audio` |
| Blender and web scenes | `eks-harness:eks-scenes` |
| Sync takes, scenes, edit and song to one clock | `eks-harness:eks-scores` |
| Add ops, macros, resource kinds, checks, effects, app runtimes, backends, plugins | `eks-harness:eks-extending` |
| Drive the harness from MCP: one-shot ops, sessions, exporting a session as a flow | `eks-harness:eks-mcp` |
| Port code written for the earlier harness API | `eks-harness:eks-migration` |
| Hand evidence to a PR or a person | `eks-harness:evidence` |
| Look at a recording or render without opening frames | `eks-harness:video-review` |
| Pools, leases, devices and browsers | `eks-harness:devices` |
| Render on GPUs elsewhere | `eks-harness:nodes` |
| Something is broken | `eks-harness:troubleshooting` |

The op reference is generated from the registry: `docs/reference/ops.md` (every op with its Python call,
platforms, arguments and examples), `catalog.md`, `checks.md`, `presets.md`, `resources.md`, `ir.md` and
`cli.md` in the eks-harness repository (`eks-harness docs --out DIR` writes them). `eks-harness ops <name>`
and `eks_ops(name=...)` give the live schema of the hub you talk to, including plugin ops.

## Not available yet

These are designed but not built; do not promise them: the Blender scene graph executor, web scene and
score take execution under `eks-harness run` (they plan and validate, and run only with `--fake`), pinned
takes, live preview, the recorder (`eks-harness record`), the plan debugger page in the web UI, plan diff
(`plan --diff`), and the visual regression, performance budget and exploration checks (they plan; at run
time they report `check_unsupported` or `op_unsupported`). The earlier video, score and studio tools stay
until then (skills `video-editing`, `scores`, `blender-scenes`, `web-scenes`).

## Essentials

- `eks-harness` and `ehx` are the same CLI. `--json` prints plain JSON. Exit codes: 0 passed, 1 failed or
  invalid plan, 2 usage or build error, 3 hub down, 4 not logged in, 6 not found, 7 lease gone, 12 run paused
  without a terminal (attach with `eks-harness debug <id>`), 13 aborted.
- A repo opts in with `.harness/project.toml` (project id, trusted plugins, `[claude]` hook settings) and
  per-app `<app>/.harness/app.toml`.
- A run acquires its leases and backend itself and releases them when it ends. Pass `--backend <lease>` to
  share a backend you already hold.
- Never start a harness the user did not ask for; when a UI change stays unverified, say so.
- Read every screenshot and look at every recording (contact sheet) you produce before you report; the
  plugin's Stop hook enforces it.
