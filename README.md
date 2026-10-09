# eks-harness

A workbench for apps under test and the videos made of them. One hub lends out browsers, iOS simulators,
Android emulators and backends, drives them with flows, keeps every capture as evidence, renders videos,
Blender scenes and HTML scenes, and synchronizes all of them on one score clock. Nodes add GPUs and
machines. Everything app- or company-specific is a plugin.

## Install

```bash
uv tool install --python 3.13 "git+https://github.com/eksenova/eks-harness"   # needs Node.js 22+ and pnpm for the UI
eks-harness setup                     # config, admin user and API key, service
eks-harness doctor
```

Updates come from the same GitHub source: `eks-harness update` installs the newest commit of `main`
(keeping the installed extras) and restarts the daemon gracefully; `eks-harness update --check` only
looks. The daemon also updates itself when nothing is running (`update.auto`, docs/updates.md).

Optional extras: `eks-harness[video]` (the video engine), `[html]` (web scenes), `[ml]` (beat tracking,
speech to text), `[matting]`, `[sam3]`, `[stems]`, `[denoise]`, `[all]`. Blender is optional and found
on the hub or on nodes.

Claude Code plugin (skills, agents, hooks, commands and the MCP server):

```text
/plugin install eks-harness --marketplace eksenova/eks-harness
```

## Concepts

| | |
| --- | --- |
| Hub | the daemon: API, web UI, MCP over stdio and HTTP, artifact store, leases, scheduler |
| Lease | exclusive use of a browser profile, device, backend slot or node slot by an instance |
| Driver | controls a target through act, observe, capture and events (web, iOS, Android, plugins) |
| Flow | a Python file with `flow(app)` that runs a whole scenario and reports checks and captures |
| Artifact | any stored output with links, tags, retention and share links |
| Backend | a service stack an app talks to, run and seeded by a plugin |
| Score | one timeline for video edits, Blender scenes, web scenes, device takes and audio |
| Review | a video's contact sheet (one image) and machine checks against an expected timeline (`docs/video-review.md`) |
| Node | a machine that dials the hub and runs jobs in GPU and CPU slots |
| Plugin | a folder or package with `harness-plugin.toml` that contributes anything above |

## A repository opts in

```text
.harness/
  project.toml          project id, enabled and trusted plugins, scores, Claude hook settings
  plugins/<name>/       repo plugins: backends, seeders, logins, app helpers, UI modules
  scores/               score.py files
<app>/.harness/
  app.toml              platforms, URLs, Metro port, fixtures, checks
  app.py                app helpers and boot()
  flows/*.py            flows
```

```bash
eks-harness flow run web/.harness/flows/checkout-check.py
eks-harness score render .harness/scores/launch/score.py
eks-harness plugin new backend acme.api .harness/plugins/api
```

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
