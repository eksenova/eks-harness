---
name: plugin-development
description: Create, test and ship eks-harness plugins: harness-plugin.toml manifests, all contribution types (drivers, platforms, backends, seeders, credentials, logins, driver extensions, flow helpers, effects, transitions, renderers, scene engines, score tracks, node jobs, MCP tools, CLI, API routes, UI modules, skills), discovery and trust, settings, scaffolding and dev reload. Use for any new integration or repo-specific automation.
---

# Plugin development

```bash
eks-harness plugin new list
eks-harness plugin new backend acme.api .harness/plugins/api
eks-harness plugin validate .harness/plugins/api
eks-harness plugin dev .harness/plugins/api
eks-harness plugin list
eks-harness plugin show acme.api
eks-harness plugin trust acme.api
eks-harness plugin install github:acme/harness-plugins#backends/api@v2
```

Manifest:

```toml
[plugin]
id = "acme.api"
name = "Acme API"
version = "0.1.0"
api = "1"
enabled_by_default = true

[requires]
python = ["httpx>=0.27"]
bin = ["docker"]

[[contributes.backend]]
id = "api"
entry = "acme_api.backend:Backend"

[[contributes.driver_extension]]
path = "web/auth.mjs"
platform = "web"

[[contributes.ui]]
slot = "backend.inspector"
module = "ui/dist/index.js"

[config]
image = { type = "str", default = "acme/api:dev", description = "container image" }
```

Contribution types: driver, driver_extension, flow_helpers, platform, device_pool, app_profile,
backend, backend_policy, seeder, credentials, login, fake, capture, annotator, viewer, effect, transition,
easing, marker_source, captioner, media_provider, media_renderer, scene_engine, score_track, encoder,
node_capability, node_job, mcp_tools, mcp_resources, cli, api, ui, skill, hook, agent, template.

- Discovery: built-ins, installed packages (`eks_harness.plugins` entry point), `plugins.paths`, repo
  `.harness/plugins/*`, git specs (`plugins.git`). Later sources override earlier ones by id.
- Trust: folder and git plugins wait for approval, pinned by content hash; `.harness/project.toml`
  `[plugins] trust = ["api"]` pre-trusts repo plugins. Settings: manifest defaults, then hub
  `plugins.settings`, then `[plugins.settings."<id>"]` in the repo.
- Python classes receive a `PluginContext` (settings, paths, data and cache dirs, logger). Protocols are in
  `eks_harness.plugins.sdk`. UI modules build against `@eks-harness/ui-sdk` and follow the
  `eks-harness:eks-harness-ui-design` skill.
