# eks-harness

A workbench for apps under test and the videos made of them. You describe what should happen in Python with
the `eks` API: drive a web or mobile app, seed its backend, check what it shows, record it, annotate it, cut it into
a video. eks-harness traces that code into a typed plan, and its hub runs the plan on leased browsers, iOS
simulators, Android emulators and backends, keeps every capture as evidence and renders the media. Nodes add GPUs
and machines. Everything app- or company-specific is a plugin.

```python
import eks
from eks import by, expect


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def subeler(s: eks.Session):
    branches = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
    kadikoy = branches.items.find(name="Kadıköy").id
    s.ui.nav("/tenant/settings/branches")
    expect(by.text("Kadıköy")).visible()
    s.checkpoint("liste")
    s.ui.nav(f"/tenant/invoice/list?branchId={kadikoy}")
    expect(by.test_id("invoice-row")).count(at_least=1)
```

```bash
eks-harness plan web-app/.harness/flows/subeler.py    # build and validate the plan, nothing runs
eks-harness run web-app/.harness/flows/subeler.py     # run it on the hub and follow it
```

## Install

```bash
uv tool install --python 3.13 --link-mode copy "git+https://github.com/eksenova/eks-harness"   # needs Node.js 22+ and pnpm for the UI and workers
eks-harness setup                     # config, admin user and API key, service
eks-harness doctor
```

Updates come from the same GitHub source: `eks-harness update` installs the newest commit of `main` (keeping the
installed extras) and restarts the daemon gracefully; `eks-harness update --check` only looks. The daemon also
updates itself when nothing is running (`update.auto`, [docs/updates.md](docs/updates.md)).

Optional extras: `eks-harness[video]` (edit, gfx and audio rendering), `[html]` (web scenes), `[ml]` (beat
tracking, speech to text, face tracking), `[matting]`, `[sam3]`, `[stems]`, `[denoise]`, `[download]`, `[all]`.
Blender is optional and found on the hub or on nodes.

Claude Code plugin (skills, agents, hooks, commands and the MCP server), from the GitHub marketplace
`eksenova/eks-harness`:

```text
/plugin marketplace add eksenova/eks-harness
/plugin install eks-harness@eks-harness
```

To share it with a team, commit this to the consuming repository's `.claude/settings.json`; Claude Code offers the
marketplace and plugin when a collaborator trusts the folder (or each one runs
`claude plugin install eks-harness@eks-harness --scope project`):

```json
{
  "extraKnownMarketplaces": {
    "eks-harness": { "source": { "source": "github", "repo": "eksenova/eks-harness" }, "autoUpdate": true }
  },
  "enabledPlugins": { "eks-harness@eks-harness": true }
}
```

The plugin's version (`claude/.claude-plugin/plugin.json`, mirrored in `.claude-plugin/marketplace.json`) is what
tells installed copies to update, so bump both when the plugin changes. The plugin bundles only the eks-harness MCP
server.

## Concepts

| | |
| --- | --- |
| Plan | the typed, hashed graph an `@eks.flow`, `@eks.seed`, `@eks.edit`, `@eks.scene` or `@eks.score` function traces into; tracing does no IO |
| Run | one execution of a plan on the hub, with a journal, a report, artifacts and a debugger |
| Op | one step of a plan (`ui.tap`, `api.get`, `edit.render`, ...); the built-in ops and plugin ops share one registry |
| Hub | the daemon: HTTP API, web UI, MCP, artifact store, pools, leases, backends, the engine that runs plans |
| Lease | use of a resource: exclusive and short for browser profiles, devices and node slots; shared and reference-counted for backends |
| Backend | a service stack an app talks to, started, health-checked, stopped and cleaned up by the hub; one instance serves every lease on it |
| Seed | a resource graph built into a backend, snapshotted and restored; it also produces the personas flows log in as |
| Artifact | any stored output with links, tags, retention and share links |
| Node | a machine that dials the hub and runs jobs in GPU and CPU slots |
| Plugin | a folder or package with `harness-plugin.toml` whose Python modules register ops, macros, resources, checks, effects, backends, app runtimes and presets |

## A repository opts in

```text
.harness/
  project.toml          project id, trusted plugins, plugin paths, backend settings, Claude hook settings
  project.local.toml    untracked per-machine or per-worktree [vars] (for example a sibling repo's path)
  plugins/<name>/       repo plugins: harness-plugin.toml and its Python modules (app runtimes, backends, resources, macros)
  flows/ seeds/ edits/ scenes/ scores/
<app>/.harness/
  app.toml              name, platforms, URLs, Metro port, bundle ids, backend
  flows/*.py            flows of this app
```

```bash
eks-harness run .harness/seeds/standard.py::standard          # build and snapshot a seed
eks-harness run web-app/.harness/flows/subeler.py --repeat 2   # run twice and compare
eks-harness run .harness/edits/demo.py                         # render an edit
eks-harness backend acquire --definition api --shell           # hold a shared backend for manual work
eks-harness do api.get path=/api/health --backend api         # one op, no file
```

## Documentation

* [docs/overview.md](docs/overview.md): how the parts fit
* [docs/cookbook.md](docs/cookbook.md): every example, with its source
* [docs/reference/](docs/reference/): generated reference of ops, catalogs, checks, presets, resources, the plan IR
  and the CLI (`eks-harness docs --out docs/reference` regenerates it)
* [docs/determinism.md](docs/determinism.md), [docs/migration.md](docs/migration.md),
  [docs/drivers.md](docs/drivers.md), and the full index in [docs/README.md](docs/README.md)

## CLI

`eks-harness` (alias `ehx`). Plans and runs: `plan`, `run`, `runs`, `debug`, `do`, `ops`, `schema`, `seed`,
`docs`. Hub and resources: `setup`, `doctor`, `daemon`, `config`, `update`, `login`, `lease`, `devices`,
`profiles`, `browsers`, `capture`, `backend`, `driver`, `reap`, `node`, `job`, `plugin`, `mcp`. Evidence:
`projects`, `sessions`, `artifacts`, `share`, `search`, `annotate`. Media kept until scenes and scores move to the
new API: `video`, `studio`, `score`, `queue`. `eks-harness help <command>` shows details, `--json` prints plain
JSON, and [docs/reference/cli.md](docs/reference/cli.md) lists every option.

## Development

```bash
uv sync                                   # Python (needs pnpm on PATH for the web build, or EKS_HARNESS_SKIP_WEB=1)
pnpm install                              # web app, SDKs and workers
EKS_HARNESS_SKIP_WEB=1 uv run pytest -q   # Python tests
pnpm -r test                              # TypeScript tests
uv run eks-harness docs --check           # the generated reference is current
uv run python web/tools/preview.py up     # isolated hub with seed data for UI work
```

Layout: `src/eks` (the authoring API), `src/eks_harness` (IR, engine, executors, hub, CLI, MCP, pools, backends,
store, media, plugins), `web` (UI), `sdk/react-native`, `sdk/scene`, `sdk/ui`, `workers` (driver workers),
`claude` (Claude Code plugin), `examples`, `docs`.
