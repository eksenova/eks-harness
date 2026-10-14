# Plugins

## Manifest

```toml
[plugin]
id = "acme"
name = "Acme"
version = "0.1.0"
api = "1"
description = "Ops, macros and the app runtime of the Acme apps"
modules = ["acme_ops", "acme_apps"]       # Python modules imported into the tree registry
python_path = [".", "src"]                # import roots inside the plugin folder (default)

[requires]
python = ["httpx>=0.27"]
bin = ["docker"]

[[contributes.driver_extension]]
path = "web/ops.mjs"                      # a JS module: export default function extend(kit) { kit.defineOp(...) }
platform = "web"

[[contributes.ui]]
slot = "backend.inspector"
module = "ui/dist/index.js"

[config]
image = { type = "str", default = "acme/api:dev", description = "container image" }
```

- `modules` carry everything decorated with `@eks.*`. The `[[contributes.*]]` tables remain for the parts
  that are not Python decorators: `driver_extension` (JS for the web or mobile worker), `ui` and `viewer`
  (web UI modules, built against `@eks-harness/ui-sdk`, following skill `eks-harness:eks-harness-ui-design`),
  `cli`, `api`, `mcp_tools`, `node_job`, `node_capability`, `encoder`, and the media contributions the
  earlier video engine still reads (`marker_source`, `captioner`, `media_provider`, `media_renderer`,
  `scene_engine`, `score_track`, `video_check`).
- Setting types: `str`, `int`, `float`, `bool`, `path`, `secret`, `list[str]`, `json`. Values come from the
  manifest default, then the hub (`eks-harness plugin set`), then `[plugins.settings."<id>"]` in the repo's
  `.harness/project.toml`.

## Discovery and trust

Plugins come from the built-ins, installed packages (`eks_harness.plugins` entry point), `plugins.paths`,
the repo's `.harness/plugins/*` and git specs (`plugins.git`); later sources win by id. Folder and git
plugins wait for approval, pinned by content hash; `[plugins] trust = ["acme"]` in `.harness/project.toml`
pre-trusts repo plugins. A changed plugin needs approval again.

Each tree gets its own registry built from the built-ins, that tree's trusted plugins and its entry files,
so two worktrees never see each other's extensions. The plugin's version and content digest are recorded
in the plan (`requires`) and in every cache key of a node that uses it.

## Commands

```bash
eks-harness plugin new list                              # templates: op, macro, backend, seeder, credentials, cli, api, ui, ...
eks-harness plugin new op acme.ops .harness/plugins/acme
eks-harness plugin new macro acme.login .harness/plugins/acme-login
eks-harness plugin validate .harness/plugins/acme       # no hub needed
eks-harness plugin dev .harness/plugins/acme            # reload on every change
eks-harness plugin list | plugin show acme | plugin trust acme
eks-harness plugin install github:acme/harness-plugins#backends/api@v2
eks-harness ops --family acme                           # what the plugin registered
eks-harness plan <file> --no-plugins                    # plan without the tree's plugins
```

## Backend plugin hooks

An `@eks.backend` class implements `describe()`, `fingerprint(context)` (a change replaces the instance),
`prepare(context)`, then `spec(context)` (processes the hub supervises, returning `BackendSpec(processes=[...],
info=BackendInfo(...))`) or `start(context)` (self-managed), and optionally `health`, `info`, `stop`,
`cleanup`, `teardown(context, final)`, `persona_headers` and the seed hooks (`reset`, `seed_phase`,
`snapshot`, `restore`, `migrations`, `drop_snapshot`). Wrap secrets in `Secret(...)`. Protocols and models
are in `eks_harness.plugins.sdk`.
