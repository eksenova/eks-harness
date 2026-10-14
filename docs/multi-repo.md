# Multiple consumer repos on one hub

One eks-harness hub (daemon, web UI, CLI and MCP) serves every repository on a machine at the same time,
including several git worktrees of the same repository. This page covers what is per user, what is per
repository or worktree, and the patterns that keep them apart.

## One daemon per user

- Install eks-harness once per user (`uv tool install ...`, then `eks-harness setup`) and run one daemon,
  ideally as the login service (`eks-harness daemon install`, label `dev.eks-harness.daemon` on macOS).
- Every repository talks to that daemon: the CLI and the stdio MCP server (`eks-harness mcp`, what the
  Claude plugin starts) are thin clients of its API. The daemon's own working directory does not matter.
- When the daemon is down, CLI and MCP calls fail with exit code 3. The Claude plugin's `SessionStart` hook
  checks once per session: if the login service is installed it asks the service to start the daemon,
  otherwise it only tells the user to run `eks-harness daemon start`. It never starts an unmanaged daemon.
  `[claude] daemon_autostart = false` makes it report only; `[claude.hooks] daemon_check = false` turns it off.

## Project ids

Each repository names its hub project in `.harness/project.toml`:

```toml
[project]
id = "acme/web"          # owner/name, shared by every worktree of the repository
```

Sessions (usually the branch) live inside the project, so two worktrees on different branches produce two
sessions of the same project. Two repositories that form one product (a frontend and a backend) keep their
own project ids; a session can span both.

## Worktrees

A worktree is a separate *tree* for the hub: plugins, backends, flows and MCP plugin tools are resolved from
the tree a command runs in.

- CLI: the tree is found from the current directory (the nearest `.harness/project.toml`, else the git top
  level).
- stdio MCP (the Claude plugin): the plugin passes `EKS_HARNESS_TREE=${CLAUDE_PROJECT_DIR}` to
  `eks-harness mcp`, so each Claude session uses the tree it was started in. Without it the server's working
  directory is used. Every tool that takes `tree` still accepts an explicit path.
- MCP over HTTP (`<hub>/mcp/`): one server in the daemon serves every repository, so the client names its
  tree per request with the `X-Eks-Harness-Tree` header or `?tree=` on the URL (an absolute path on the hub
  machine). Plugin MCP tools are then the ones that tree's plugins contribute, `tree` arguments default to
  it, and `eks_run` and `eks_flows` work in it. Without a tree only hub-wide plugin tools are listed and the
  tools that need a tree ask for one. Prefer the stdio server for local repositories.

```json
{
  "mcpServers": {
    "eks-harness": {
      "type": "http",
      "url": "http://hub.local:7171/mcp/",
      "headers": { "Authorization": "Bearer ${EKS_HARNESS_API_KEY}", "X-Eks-Harness-Tree": "${PWD}" }
    }
  }
}
```

## Plugin trust

Folder and git plugins wait for approval, pinned by content hash. For repositories with many worktrees:

- Pre-trust the repository's plugins in `.harness/project.toml`. Entries are plugin ids or paths relative to
  `.harness/plugins/`; they apply to the repo plugins, to plugins the same file loads through
  `[plugins] paths`, and to its `[plugins] git` plugins, in every worktree, whatever their content hash.

  ```toml
  [plugins]
  trust = ["acme.web", "acme.api"]
  ```

- Without pre-trust, approving a plugin once (`eks-harness plugin trust <id>` or the UI) also covers every
  other checkout of the same plugin id whose content hash is identical, so a fresh worktree of an approved
  commit loads directly. A worktree whose plugin code differs waits for its own approval.

## Cross-repo plugins

A repository can load a plugin that lives in another local repository, for example a frontend that needs the
backend contributed by the backend repository's plugin. List it in `[plugins] paths`:

```toml
[vars]
BACKEND_TREE = "${main_tree}/../acme-api"

[plugins]
paths = ["${BACKEND_TREE}/.harness/plugins/acme-api"]
trust = ["acme.api"]
```

- Relative paths resolve against the tree root (paths relative to `.harness/` keep working when nothing
  exists at the tree-relative location).
- Placeholders: `${tree}` is the current worktree, `${main_tree}` the repository's main checkout (stable
  across worktrees, so a worktree under `.claude/worktrees/` still finds the sibling repository),
  `${NAME}` a variable, `${NAME:-fallback}` a variable with a fallback that may itself hold placeholders.
- Variables come from, highest first: `.harness/project.local.toml` `[vars]` (untracked; add it to
  `.gitignore`), the environment of the process that reads the config, then `[vars]` in `project.toml`.
  The daemon runs with the login service's environment, not your shell's, so pin per-machine or
  per-worktree choices in `project.local.toml` rather than in a shell variable:

  ```toml
  # .harness/project.local.toml in a frontend worktree that should use a backend worktree
  [vars]
  BACKEND_TREE = "/Users/me/src/acme-api/.claude/worktrees/feature-x"
  ```

- An unresolved placeholder shows up as a plugin error in `eks-harness plugin list` and the UI.
- A git source (`[plugins] git = ["github:acme/acme-api#.harness/plugins/acme-api@main"]`, fetched with
  `eks-harness plugin sync`) suits a pinned version that does not follow a local checkout.
- The plugin's code always runs from its own folder: `context.root` is inside the other repository, so the
  plugin finds that repository's sources from there, while `context.tree` is the consumer's tree.

## Shared backends

A backend instance belongs to its definition and the tree it runs from, not to a session: every Claude
session, worktree and repository that acquires `api` from `/src/acme-api` shares `api@tree:/src/acme-api`.
A frontend worktree points at a backend worktree by naming that tree:

```bash
eks-harness backend acquire --definition api --backend-tree "$BACKEND_TREE"   # prints the lease id and URL
eks-harness run web/.harness/flows/x_check.py --backend api --backend-tree "$BACKEND_TREE"
```

or in `<app>/.harness/app.toml`:

```toml
[backend]
definition = "api"
tree = "${BACKEND_TREE}"        # relative paths are relative to the app; ${...} come from the environment
[web]
api_url = "${EKS_BACKEND_URL}"  # filled from the linked backend's info
```

- Agents take one backend lease (or rely on driver links, which hold one each) and keep driver leases
  short; the instance stays up across them and stops after its idle timeout once nobody holds it.
- Set the idle timeout per repository in the backend's tree: `[backends] idle_timeout = "30m"`, or per
  definition `[backends.api] idle_timeout = "never"`. `--key` and `--param` make separate instances of
  the same tree (for example one per tenant).
- `eks-harness backend leases` and the Backends page show who holds what; `backend info` shows the URLs,
  commit and branch (credentials only to a holder with `--secrets`).

## Python module isolation

The daemon is one process shared by every repository, so plugin modules are loaded per plugin folder and
content: each folder and content hash gets its own private module namespace. Two worktrees whose plugins
both ship `acme_harness/` each get their own code, and an edited plugin loads as a new generation on its
next use without restarting the daemon.

- Absolute imports inside plugin code (`from acme_harness.paths import x`) and relative imports resolve
  within the plugin's own folder.
- Code outside plugins that imports a plugin package by its plain name (a flow's `app.py` that puts the
  plugin folder on `sys.path`) gets the same module object the plugin uses.
- `importlib.import_module("acme_harness.x")` called from plugin code bypasses the namespace; use a normal
  import statement.
- Superseded generations stay loaded until the daemon restarts. Third-party requirements (`[requires]
  python`) share one environment per requirement set, so two plugins that pin different versions of the same
  library still collide.

## Claude Code plugin per repository

Install the plugin team-wide by committing `.claude/settings.json` in each consumer repository (see the
README). Hook behaviour is configured per repository in `.harness/project.toml`:

```toml
[claude]
ui_paths = ["src/web/"]                  # PR evidence guard: these paths need UI evidence in the PR body
ui_design_paths = ["src/web/components/"] # design hint: files governed by a design system
ui_design_skill = "acme:design-system"    # the skill the design hint points at
harness_words = ["harness", "simulator"]
daemon_autostart = true

[claude.hooks]
evidence = true
cheap_inspection = true
pr_evidence = true
design_hint = false
prewarm = false
daemon_check = true
```

- The design hint fires only where the repository opts in: `ui_design_paths` set, or
  `[claude.hooks] design_hint = true` (which uses the harness's own UI paths). `ui_design_paths = []`
  turns it off.
- The Bash guards run only for matching commands (`eks-harness ...`, `ehx ...`, `gh pr ...`,
  `git worktree add ...`).
- The plugin no longer bundles a Playwright MCP server; eks-harness drives browsers itself. A repository that
  wants Playwright MCP adds it to its own `.mcp.json`.
- Any hook can be switched off for one shell with `EKS_HARNESS_HOOK_<NAME>=0`.

## Settings that are still hub-wide

| Setting | Scope | Notes |
| --- | --- | --- |
| `devices.*` (pool sizes, device type, name prefix, AVDs) | hub | The device pool is one physical resource shared by every repository; per-project app cleanup is a project setting in the hub (`apps.iosBundleIds`, `apps.androidPackages`, `devices.strayProcessPatterns`). |
| `browser.*` (browser command, processes, profiles) | hub | One browser pool; profiles are leased per instance, so repositories never share a live profile. |
| `backend.portRangeStart/End` | hub | Ports are handed out by the one daemon. |
| `backend.idleTimeout` | hub default, project and definition override | `[backends] idle_timeout` / `[backends.<definition>] idle_timeout` in the backend tree's `.harness/project.toml`, then `idle_timeout` on the manifest or `describe()`, win over the hub default (`15m`). |
| `backend.leaseTtlSeconds`, `backend.readyTimeoutSeconds` | hub | Default lease ttl when the holder names none; how long a linked driver lease waits for its backend. |
| `backend.definitionDirs` | hub default, project override | `[backends] definition_dirs = ["ops/backends"]` in `.harness/project.toml` replaces the hub default for that tree. |
| `plugins.paths`, `plugins.git`, `plugins.enabled`, `plugins.disabled`, `plugins.settings` | hub | Repository plugins, enablement and settings belong in `.harness/project.toml` (`[plugins]`, `[plugins.settings.<id>]`), which override these. |
| `capture.*`, `server.*`, `auth.*`, `render.*`, `update.*` | hub | Not repository concerns. |

Pools stay hub-wide on purpose: making them per project would split one set of simulators, emulators and
browsers into competing pools on the same machine.
