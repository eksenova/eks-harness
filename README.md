# eks-harness

A workbench for apps under test and the videos made of them. One hub lends out browsers, iOS simulators,
Android emulators and backends, drives them with flows, keeps every capture as evidence, renders videos,
Blender scenes and HTML scenes, and synchronizes all of them on one score clock. Nodes add GPUs and
machines. Everything app- or company-specific is a plugin.

## Install

```bash
uv tool install --python 3.13 --link-mode copy "git+https://github.com/eksenova/eks-harness"   # needs Node.js 22+ and pnpm for the UI
eks-harness setup                     # config, admin user and API key, service
eks-harness doctor
```

Updates come from the same GitHub source: `eks-harness update` installs the newest commit of `main`
(keeping the installed extras) and restarts the daemon gracefully; `eks-harness update --check` only
looks. The daemon also updates itself when nothing is running (`update.auto`, docs/updates.md).

Optional extras: `eks-harness[video]` (the video engine), `[html]` (web scenes), `[ml]` (beat tracking,
speech to text), `[matting]`, `[sam3]`, `[stems]`, `[denoise]`, `[all]`. Blender is optional and found
on the hub or on nodes.

Claude Code plugin (skills, agents, hooks, commands and the MCP server), from the GitHub marketplace
`eksenova/eks-harness`:

```text
/plugin marketplace add eksenova/eks-harness
/plugin install eks-harness@eks-harness
```

To share it with a team, commit this to the consuming repository's `.claude/settings.json`; Claude Code
offers the marketplace and plugin when a collaborator trusts the folder (or each one runs
`claude plugin install eks-harness@eks-harness --scope project`):

```json
{
  "extraKnownMarketplaces": {
    "eks-harness": { "source": { "source": "github", "repo": "eksenova/eks-harness" }, "autoUpdate": true }
  },
  "enabledPlugins": { "eks-harness@eks-harness": true }
}
```

The plugin's version (`claude/.claude-plugin/plugin.json`, mirrored in `.claude-plugin/marketplace.json`)
is what tells installed copies to update, so bump both when the plugin changes. The plugin bundles only the
eks-harness MCP server; add Playwright MCP to a repository's own `.mcp.json` if it wants one.

Several repositories and worktrees share one hub: see `docs/multi-repo.md` for project ids, trust,
cross-repo plugins, module isolation and per-repository hook settings.

## Concepts

| | |
| --- | --- |
| Hub | the daemon: API, web UI, MCP over stdio and HTTP, artifact store, leases, scheduler |
| Lease | use of a resource by an instance: exclusive and short for browser profiles, devices and node slots; shared and reference-counted for backends |
| Driver | controls a target through act, observe, capture and events (web, iOS, Android, plugins) |
| Flow | a Python file with `flow(app)` that runs a whole scenario and reports checks and captures |
| Artifact | any stored output with links, tags, retention and share links |
| Backend | a service stack an app talks to, started, health-checked, stopped and cleaned up by its project's plugin; one instance serves every lease on it and stops after its idle timeout once none is left |
| Score | one timeline for video edits, Blender scenes, web scenes, device takes and audio |
| Review | a video's contact sheet (one image) and machine checks against an expected timeline (`docs/video-review.md`) |
| Node | a machine that dials the hub and runs jobs in GPU and CPU slots |
| Plugin | a folder or package with `harness-plugin.toml` that contributes anything above |

## A repository opts in

```text
.harness/
  project.toml          project id, enabled and trusted plugins, plugin paths, scores, Claude hook settings
  project.local.toml    untracked per-machine or per-worktree [vars] (for example a sibling repo's path)
  plugins/<name>/       repo plugins: backends, seeders, logins, app helpers, UI modules
  scores/               score.py files
<app>/.harness/
  app.toml              platforms, URLs, Metro port, fixtures, checks
  app.py                app helpers and boot()
  flows/*.py            flows
```

```bash
eks-harness backend acquire --definition api            # shared backend lease; prints the lease id and URL
eks-harness flow run web/.harness/flows/checkout-check.py --backend api --release
eks-harness backend release bl_0123456789abcdef
eks-harness score render .harness/scores/launch/score.py
eks-harness plugin new backend acme.api .harness/plugins/api
```

Hold a backend lease (or let driver leases link one with `--backend`), keep driver leases short and
release both when done; the backend keeps running between driver leases and is stopped and cleaned up
after its idle timeout once nothing holds it (`docs/spec.md` section 10).

## CLI

`eks-harness` (alias `ehx`): `setup`, `doctor`, `daemon`, `config`, `login`, `projects`, `sessions`,
`artifacts`, `share`, `search`, `annotate`, `lease`, `devices`, `profiles`, `browsers`, `capture`,
`backend`, `flow`, `driver`, `node`, `job`, `plugin`, `video` (including `video sheet` and `video check`), `studio`,
`score`, `migrate`, `mcp`.
`eks-harness help <command>` shows details; `--json` prints plain JSON.

## Development

```bash
uv sync                                   # Python (needs pnpm on PATH for the web build, or EKS_HARNESS_SKIP_WEB=1)
pnpm install                              # web app, SDKs and workers
EKS_HARNESS_SKIP_WEB=1 uv run pytest -q   # Python tests
pnpm -r test                              # TypeScript tests
uv run python web/tools/preview.py up     # isolated hub with seed data for UI work
```

Layout: `src/eks_harness` (hub, CLI, drivers, flows, video engine, scores, nodes, plugins, MCP),
`web` (UI), `sdk/ui`, `sdk/scene`, `sdk/react-native`, `workers` (driver workers), `claude` (Claude Code
plugin), `docs`.
