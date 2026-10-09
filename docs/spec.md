# eks-harness: specification

Status: version 1. This document is the contract for eks-harness: a standalone, generic, plugin-driven
harness that unifies UI testing and capture (web, iOS, Android), a video engine, Blender rendering, HTML/JS
scenes and synchronized "scores" that tie all of them to one clock.

## 1. Goals

1. A standalone project with no references to any particular company, product, host name or machine in
   code, defaults, docs or tests. Everything environment-specific lives in user config, project config or
   plugins.
2. Generic: any app, any backend, any project. Modular, with plugins for drivers, devices, platforms,
   backends, seeders, credentials, effects, transitions, markers, captioners, media renderers, scene
   engines, MCP tools, CLI commands, skills and UI extensions. Plugins can live anywhere (installed packages,
   folders, other repositories).
3. The video engine, its studio and its Blender integration are native parts of eks-harness. Blender stays
   optional. A video edit, Blender scene scripts, HTML/JS scenes and device or browser scripts can be authored
   together and synchronized to the same beats and cues, in both directions.
4. A Claude Code plugin bundles skills, hooks, agents and a comprehensive MCP server.
5. Existing installations migrate with their data (users, keys, projects, sessions, artifacts, share links,
   settings, devices, browser profiles, render farms, studio renders).
6. A distinct UI designed from research, documented in a UI/UX design skill, responsive, extendable by
   plugins, and reviewed with screenshots before it is accepted.

## 2. Decisions

| Topic | Decision |
| --- | --- |
| Video engine | Lives in the monorepo as `eks_harness.video`; projects written for its predecessor are migrated, there is no compatibility shim. |
| Git history | Fresh repository. |
| Host names | An optional share host (`server.shareUrl`) serves only public share routes; the app host (`server.publicUrl`) serves the UI, API, studio and MCP over HTTP behind the harness's own auth. Tunnel tokens never appear on command lines. |
| Drivers | Typed driver plugins behind a driver protocol. Built-in drivers: web (Playwright, TypeScript worker), iOS (simctl), Android (adb/emulator), each with an optional React Native DevTools bridge. App specifics move to plugins. |
| React Native bridge | `@eks-harness/react-native` in the monorepo, installed by apps as a git dependency; app-specific fakes register through its API. |
| Consumption | A repository uses eks-harness as an installed uv tool plus the Claude plugin from its marketplace, and may add it as a git submodule to develop the harness alongside the app. |
| Sync model | One score (shared timeline of tracks and events) with a frame-exact render mode and a real-time live mode. |
| Authoring | Python DSL that serializes to a JSON IR; the studio edits the same IR. |
| Web scenes | JS/TS allowed, on a virtual clock (Date, performance.now, rAF, timers, CSS and Web Animations) so renders are frame-exact. |
| Plugins | A folder or package with `harness-plugin.toml` declaring contributions; discovered from entry points, configured paths, repo `.harness/plugins/` and git URLs; enabled per project. |
| Plugin trust | Trust on first use: built-ins and installed packages load directly; folder and git plugins wait for approval (CLI or UI), pinned by content hash; project config can pre-trust its repo plugins. |
| UI extensions | Prebuilt ES modules loaded at runtime into named slots through a typed UI SDK, sharing React and the design tokens. |
| UI | New design system and one unified app (harness plus studio, score timeline, live device control, scene previews); React 19, TanStack Router and Query, Vite, TypeScript, plain CSS with tokens, no component kit. |
| Topology | One hub (this Mac) plus nodes that dial out to it over WebSocket (`eks-harness node serve`), installed and updated over SSH by `eks-harness node add`. The render farm becomes node configuration. |
| Migration | Everything carries over; old share URLs keep resolving. |
| Rollout | Build the whole system next to an existing installation; switch in one step. |
| Agents and hooks | Generic agents and hooks in the Claude plugin, configurable per project; app knowledge in repo plugins and repo skills. |
| UI review | Playwright MCP configured in the repo's `.mcp.json` and in the Claude plugin; scripted Playwright until a session loads it. |
| CLI | `eks-harness`, plus the short alias `ehx`. |
| Extras | Plugin SDK with scaffolding templates (`eks-harness plugin new <kind>`). |

## 3. Architecture

```
                       share.example.com (public routes)    hub.example.com (app)
                                   \                                 /
                                    cloudflared tunnel (token file)
                                                 |
   +------------------------------------- HUB (eks-harness daemon) --------------------------------------+
   |  API (FastAPI)  auth  store  db  events  shares  MCP (stdio + HTTP)  UI (SPA + plugin slots)         |
   |  plugin host    scheduler    leases    score runtime (render + live)    video engine (eks_harness.video) |
   |  drivers: web (TS worker, Playwright) | ios (simctl) | android (adb) | RN DevTools bridge | plugin drivers |
   |  media renderers: blender | html scene | device take | plugin renderers                                 |
   +------------------------------------------------------------------------------------------------------+
            | WebSocket (node dials out)                     | WebSocket                     | ...
     node: gpu-a (3 GPUs, Blender)                  node: gpu-b (WSL, 1 GPU)         node: any (browsers, emulators, backends)
```

- The hub owns state: database, artifact store, shares, leases, events,
  sessions, plugin registry, scheduling. It runs the UI and APIs.
- Nodes own capacity: GPUs, Blender, browsers, Android emulators, Linux
  backends. A node reports capabilities and runs jobs; the hub never needs to
  reach a node directly.
- Everything app- or company-specific is a plugin or project configuration.

## 4. Repository layout

```
eks-harness/
  pyproject.toml            uv workspace root (Python 3.12+)
  package.json              pnpm workspace root
  .mcp.json                 Playwright MCP + the harness MCP (for developing the harness)
  .claude-plugin/           marketplace.json (the plugin marketplace)
  src/eks_harness/          the Python distribution "eks-harness"
    core/                   config, paths, ids, events, errors
    daemon/                 app factory, lifecycle, service install (launchd/systemd)
    api/                    HTTP routers
    auth/                   users, sessions, API keys, grants, guards
    db/                     schema, migrations, repositories
    store/                  artifact store, thumbnails, shares, retention
    leases/                 leases, instances, slots, ports
    nodes/                  node agent, hub side, sync, scheduling
    plugins/                manifest, discovery, trust, registry, contribution types, SDK
    drivers/                driver protocol + built-in drivers (web, ios, android, rn bridge client)
    backends/               backend protocol + runner
    flows/                  the Python flow SDK (successor of eksflow) and runner
    capture/                screenshots, recordings, pacing, trim, step logs, annotate
    live/                   live device and browser streams
    video/                  the video engine: ir, compile, render, effects, transitions,
                            markers, captioners, audio, media renderers, html scenes, blender, farm jobs
    score/                  score model, clock, event bus, render and live runtimes
    studio/                 studio services (previews, timeline resolve, publish)
    mcp/                    MCP server (tools, resources, prompts)
    cli/                    eks-harness / ehx
  workers/web-driver/       TypeScript Playwright worker (the generic web driver)
  workers/mobile-driver/    TypeScript worker for the RN DevTools bridge (CDP relay, evaluate, events)
  web/                      the app UI (React)
  sdk/ui/                   @eks-harness/ui-sdk (slots, hooks, tokens, components for plugin UIs)
  sdk/react-native/         @eks-harness/react-native (in-app bridge)
  sdk/scene/                @eks-harness/scene (virtual clock + event API for HTML/JS scenes)
  plugins/                  built-in plugins, each a normal plugin folder (drivers, blender, html-scene,
                            effects packs, ffmpeg transitions, captioners, whisper, sam3, matting, ...)
  templates/                plugin scaffolds (driver, device, platform, backend, effect, transition,
                            media-renderer, scene-engine, mcp-tools, ui-extension, skill)
  claude/                   the Claude Code plugin: plugin.json, skills/, agents/, hooks/, commands/
  docs/                     spec, architecture, plugin guide, score guide, UI design notes
  tests/                    Python tests; web/sdk tests live next to their packages
```

The Python distribution keeps heavy dependencies optional through extras:
`eks-harness[video]` (ffmpeg-based engine, always light), `[ml]` (beat
tracking, whisper), `[matting]`, `[sam3]`, `[stems]`, `[denoise]`, `[html]`
(Playwright for Python where needed), `[all]`. Blender needs no Python package;
it is a plugin that needs a Blender executable on a node or the hub.

## 5. Core concepts

- **Project**: `owner/name`; holds sessions, artifacts, settings, enabled
  plugins, app profiles, backends, score workspaces.
- **Session**: a working context inside a project (a branch, a ticket, a video).
- **Artifact**: any stored output (screenshot, video, DOM, HAR, logs, render,
  score snapshot, Blender frames, scene captures) with metadata, thumbnails,
  versions and shares.
- **Lease**: exclusive use of a resource (browser profile, device, backend
  slot, node GPU) by an instance (a Claude session, a CI job, a user).
- **Driver**: controls a target (browser, simulator, emulator, real device,
  desktop app) through the driver protocol.
- **Platform**: a kind of target and its device pool (web, ios, android, or
  plugin platforms such as macOS apps or Windows).
- **App profile**: how to build, install, launch and identify an app on a
  platform (bundle id, package, URL, build command, launch arguments, bridge).
- **Backend**: a service stack an app talks to (local or remote), managed by a
  backend plugin (prepare, spec, health, seed, teardown).
- **Score**: a synchronized timeline (section 9).
- **Node**: a machine contributing capacity.
- **Plugin**: a unit of extension (section 6).

## 6. Plugin system

### 6.1 Manifest

```toml
# harness-plugin.toml
[plugin]
id = "acme.backend"            # globally unique, reverse-DNS style
name = "Acme backend"
version = "1.2.0"
api = "1"                      # plugin API major version
description = "Local Acme API with seeded personas"

[requires]
python = ["httpx>=0.27"]       # installed into the plugin's environment
nodes = ["docker"]             # capabilities a node must offer to run its jobs

[[contributes.backend]]
id = "acme-api"
entry = "acme_backend.backend:AcmeBackend"

[[contributes.seeder]]
id = "acme-scenario"
entry = "acme_backend.seed:Scenario"

[[contributes.credentials]]
id = "acme-personas"
entry = "acme_backend.auth:Personas"

[[contributes.ui]]
slot = "backend.inspector"
module = "ui/dist/backend-panel.js"

[[contributes.skill]]
path = "skills/acme-backend"

[config]                        # typed settings the plugin exposes (shown in the UI)
seed_snapshot = { type = "path", default = "" }
```

### 6.2 Contribution types

`driver`, `platform`, `device_pool`, `app_profile`, `backend`, `seeder`,
`credentials`, `login`, `capture`, `annotator`, `viewer` (artifact viewers),
`effect`, `transition`, `easing`, `marker_source`, `captioner`,
`media_renderer`, `scene_engine`, `score_track`, `encoder`, `node_capability`,
`mcp_tools`, `mcp_resources`, `cli`, `ui`, `skill`, `hook`, `agent`,
`template`. Each type has a Python protocol (or TS interface for UI) in
`eks_harness.plugins.sdk` and a JSON schema for its manifest entry.

### 6.3 Discovery, enablement, trust

Discovery order (later can override earlier by id only when allowed):
1. built-in plugins (`plugins/` shipped in the distribution),
2. installed Python packages exposing the `eks_harness.plugins` entry point,
3. folders listed in user config (`plugins.paths`),
4. repo plugins: `.harness/plugins/*/harness-plugin.toml` in a project's repo,
5. git plugins: `plugins.git = ["github:org/repo#path@ref"]`, fetched into the
   cache.

Each project lists enabled plugins and their settings in its project config
(stored in the hub, optionally mirrored in the repo's `.harness/project.toml`).
Trust: built-ins and installed packages are trusted; folder and git plugins
are pending until approved (`eks-harness plugin trust <id>` or the UI), and
approval pins the content hash (a change requires re-approval).
`.harness/project.toml` may pre-trust its own repo plugins by path.

Python plugins with extra requirements get an isolated uv environment keyed by
their lockfile; plugin code that must run inside the daemon process imports
only `eks_harness.plugins.sdk`.

### 6.4 Plugin SDK and scaffolding

`eks-harness plugin new <kind> [path]` scaffolds a plugin from `templates/`
with a manifest, a typed implementation stub, tests and (for UI) a Vite build.
`eks-harness plugin dev <path>` loads a plugin live with reload on change.
`eks-harness plugin validate` checks manifests, schemas and API versions.

### 6.5 UI extensions

Plugins ship ES modules built against `@eks-harness/ui-sdk` (React and the
design tokens are shared from the host through an import map). Slots:
`nav.section`, `route` (a full page), `project.tab`, `session.panel`,
`artifact.viewer` (by artifact kind/mime), `device.controls`,
`backend.inspector`, `node.inspector`, `score.track` (renders a track type in
the timeline), `score.inspector`, `settings.page`, `command` (command palette
entries). The host passes typed context (project, session, selection, theme,
API client, event stream) and the module returns React components.

## 7. Drivers, platforms and devices

### 7.1 Driver protocol

A driver exposes:
- `session`: open/close against a leased target; health; reset.
- `act`: tap/click, type, scroll, swipe, key, navigate, open deep link, back,
  evaluate (JS in the app or page), dispatch (app state actions), arm (fake
  inputs such as camera, NFC, documents, location).
- `observe`: tree (accessibility or element tree), query/find, wait-for,
  text, state, route, logs, network, console.
- `capture`: screenshot, recording start/stop with step log and cue marks,
  HAR, DOM/MHTML, accessibility dump.
- `events`: a stream of target events (navigation, route change, log, network,
  custom app events) consumable by flows and scores.

### 7.2 Built-in drivers

- **web**: a TypeScript worker (`workers/web-driver`) driving Chrome/Chromium
  over CDP with Playwright, one worker per leased profile; screencast-based
  recording with exact timestamps; overlay for annotations.
- **ios**: simctl for boot, install, launch, record (`recordVideo`), deep
  links, media; the RN bridge for app-level control when the app embeds it.
- **android**: adb and the emulator for the same; the RN bridge likewise.
- **React Native bridge**: `@eks-harness/react-native` runs inside dev builds;
  the mobile driver worker reaches it through Metro's inspector (CDP
  `Runtime.evaluate`) and exposes tree, find, press/fill/scroll by
  test id or text, navigation (`navigate(route, params)`), store dispatch,
  arbitrary evaluate, live UI patches (props/state overrides, overlays,
  injected components) and fake media/NFC/camera/document providers that apps
  register.

Login flows, personas, impersonation, app menus and anything app-specific are
`login`/`credentials`/`app_profile` plugins, never driver code.

### 7.3 Pools

Device pools are generic and configured per platform (counts, device type,
base image, port ranges, naming prefix from config). Pools may live on nodes
(an Android emulator pool on a Linux node, browsers on a node).

## 8. Video engine (`eks_harness.video`)

The engine covers:
- IR (projects, tracks, segments, media, effects, transitions, markers,
  animated values, curves, easing), compile, render orchestration, segment
  cache, composite, audio, encoders, hardware acceleration.
- Markers: beat/downbeat tracking, onsets, energy, momentum, STT words, scene
  markers, faces, motion.
- Effects and transitions as plugins (the built-in set ships as built-in
  plugins).
- Media renderers (plugin category): Blender (`BlenderScene`), HTML/JS scene
  (`WebScene`), device take (`DeviceTake`, the successor of `HarnessCapture`),
  and plugin renderers. Media renderers can nest media (a web scene or device
  take as a Blender texture, a Blender render inside a web scene, and so on).
- Render farm: Blender and other frame jobs schedule onto nodes; the per-frame
  scene fingerprint cache and platform affinity are kept.
- Studio: project and score editing, timeline, previews, renders, publish; it
  is part of the unified UI (section 13) instead of a separate app.
- Outputs are artifacts in the store (renders, previews, frames on request)
  with share links; the studio deep link opens a render in the UI.

Blender is optional: the Blender plugin is enabled per project and only then
looks for a Blender executable (hub or node).

## 9. Scores: synchronized edits across everything

### 9.1 Model

A score is a timeline with:
- **clock**: fps, duration, tempo map (from a song's beat analysis or fixed
  BPM), named markers.
- **tracks**: `edit` (video IR tracks), `blender` (scene scripts), `web`
  (HTML/JS scenes), `device` (flows against a platform), `audio`, and plugin
  track types.
- **events**: beats, cues, triggers and data events on the shared clock.
  Any track can emit events (a web scene emits `card-flipped`, a device track
  emits `screen:result-shown`, a Blender scene emits `impact`) and any track
  can subscribe (a Blender script keys an object on `cue:match`, a device
  track presses a button on `beat:12`, a web scene animates when the device
  reports a route change).
- **bindings**: how one track's output feeds another (a web scene rendered as
  a Blender material, a device take as a phone screen texture, a Blender layer
  composited in the edit).

Authoring is a Python DSL that serializes to JSON IR:

```python
from eks_harness.score import Score, beats, cue
s = Score(fps=30, song="music/summer.ogg", window=(147.89, 175.55))
app = s.device("ios", flow="flows/scan.py")              # flow(app, events) runs on the clock
card = s.web("scenes/card/index.html")                  # JS scene on the virtual clock
phone = s.blender("scenes/phone.py", textures={"screen": app, "card": card})
s.edit.layer(phone).layer("scenes/titles.py")
s.on(beats("downbeat")[3], app.press("#start-nfc"))   # device action on a downbeat
s.on(app.event("result-shown"), card.emit("flip"))     # device -> web scene
s.on(cue("match"), phone.emit("impact"))               # cue -> Blender
```

### 9.2 Render mode (frame-exact)

- Blender, web and edit tracks are evaluated per frame; web scenes run on the
  virtual clock and receive events at their exact frame times; Blender scripts
  receive events through `eks_harness.blender`
  as scene frames.
- Device tracks run as a live **take**: the flow is executed against the real
  target while recording; actions fire at their score times on the wall clock
  (scheduled against the recorder's start), cues are stamped, and the take is
  time-warped so every cue lands on its exact frame. Device events observed
  during the take become timestamped score events that other tracks consume
  in their frame-exact evaluation.
- Order: device takes first (they produce events and footage), then web and
  Blender (consuming events and footage), then the edit composite.

### 9.3 Live mode (real time)

The score plays on a real-time clock in the UI (with audio): beats and cues
fire device actions and RN UI patches immediately, web scenes run live in the
browser, Blender previews run on a node in viewport/EEVEE mode at reduced
resolution, and events flow in every direction through the event bus. Live
sessions can be recorded and promoted to a take.

### 9.4 Interactions

Anything can trigger anything through events: beats and cues, device
observations (route changed, element appeared, network response), web scene
events, Blender emitted events (frame-exact, from keyed custom properties or
script calls), and UI actions in live mode. Bindings carry media between
engines (web as Blender texture, Blender as web scene layer, device takes on
screens).

## 10. Backends, seeding, credentials

Backends are plugins implementing `describe`, `fingerprint`, `prepare`,
`spec` (URLs, env), `health`, `seed`, `snapshot/restore` and `teardown`; the
hub runs and supervises them on the hub or a node. Seeders and credential
providers are separate contributions so a repo plugin can offer personas and
logins against any backend (local or staging). Backend choice policies (for
example "local when the branch changes the API") are plugin hooks.

## 11. Nodes

- `eks-harness node add <ssh-host>` installs the node agent (uv tool) over SSH,
  writes its config (hub URL and a node token) and installs a service
  (systemd user unit or launchd). Updates follow the hub version.
- `eks-harness node serve` dials the hub over WebSocket, reports capabilities
  (CPU/GPU list with backends such as OPTIX/CUDA/METAL, Blender path and
  version, Chrome, Android SDK, Docker, disk), and runs jobs: frame ranges,
  scene previews, browser workers, emulators, backends.
- Files move through a content-addressed cache (hub serves inputs by hash,
  nodes upload outputs as artifacts or job results); no shared filesystem or
  inbound connection needed.
- An SSH render farm configuration (`farm.toml`) migrates to nodes with `eks-harness migrate farm`.

## 12. Storage, configuration and migration

- Paths are platformdirs-based with app name `eks-harness` (`EKS_HARNESS_HOME` isolates an installation).
- Configuration: user config (hub settings), project config (in the hub,
  optionally mirrored to `.harness/project.toml` in a repo), plugin settings.
  No company defaults in code: bundle ids, AVD names, device name prefixes,
  service labels and project names come from config.
- Service labels: `dev.eks-harness.daemon` (launchd) and `eks-harness.service`
  (systemd).
- Migration: the database upgrades its schema in place (users, keys, grants, projects, sessions, artifacts,
  shares, notes, timelines); settings merge key by key (`eks-harness migrate config`); SSH farms become nodes
  (`eks-harness migrate farm`); finished studio renders become artifacts (`eks-harness migrate studio`);
  browser profiles and device pools are kept; existing share URLs keep resolving.

## 13. UI and UX

Process:
1. Research common AI-generated and "Claude-style" UI patterns and pitfalls
   (status dots everywhere, translucent blurred headers, default Lucide icon
   soup, excessive rounded corners and pill shapes, gradient accents, cards in
   cards, verbose explanatory copy and disclaimers, empty-state essays, emoji
   decoration, low-density layouts), plus what professional tools in the same
   space do well (NLE timelines, DAWs, device farms, observability tools).
2. Write the design concept and system into the repo skill
   `claude/skills/eks-harness-ui-design` (principles, tokens, typography,
   iconography, layout grid, density, motion, data display, copy rules,
   banned patterns, responsive rules, review checklist) and follow it.
3. Information architecture driven by the work: a project's live surface
   (devices, browsers, backends, nodes and what is running on them), sessions
   and evidence (artifacts with viewers and diffs), the studio (score
   timeline with all track types, previews, renders), and the system (plugins,
   nodes, settings, users).
4. Responsive from phone to wide desktop; the timeline and device views have
   deliberate small-screen modes rather than squeezed desktop layouts.
5. Review loop: Playwright screenshots at phone, tablet and desktop sizes for
   every page and state, judged against the skill's checklist before anything
   is accepted.

## 14. Claude Code plugin

The repository is a plugin marketplace (`.claude-plugin/marketplace.json`)
with one plugin, `eks-harness`, built from `claude/`:
- **MCP server** (`eks-harness mcp`, stdio and HTTP): projects, sessions,
  artifacts (search, upload, tag, notes, share, annotate, versions), leases,
  devices and browsers (acquire, release, status), driver actions (act,
  observe, capture) for interactive control, flows (run, list, report),
  backends (status, prepare, seed, logs), nodes (status, jobs), plugins (list,
  trust, settings), video (validate, inspect, render, markers, effects,
  transitions, add segment/effect, set property: the full video editing
  surface), scores (create, inspect, run take, render, live control), Blender
  (render scene, preview), studio publish and links, plus resources (effects,
  easings, templates, render jobs, preview frames, plugin catalog) and prompts.
- **Skills**: harness overview, flows (authoring and running), web, iOS,
  Android, React Native bridge, devices and pools, backends and seeding,
  evidence and PR screenshots, annotation, video editing,
  scores and synchronization, Blender scenes, web scenes, nodes and render
  farm, plugin development, UI design, troubleshooting.
- **Agents**: web, ios, android and studio/render agents, generic and driven
  by project config.
- **Hooks**: evidence inspection before finishing, cheap inspection before
  screenshots, PR UI evidence, worktree prewarm, lease release on stop, plugin
  design hint; each switchable per project.
- **Commands**: quick entry points (`/harness`, `/flow`, `/render`, `/score`).

## 15. CLI

`eks-harness` (alias `ehx`): `setup`, `daemon`, `service`, `status`,
`project`, `session`, `artifact`, `share`, `lease`, `device`, `browser`,
`backend`, `flow run`, `node add|serve|list|remove`, `plugin new|dev|list|trust|validate|install`,
`video render|validate|inspect|markers`, `score new|take|render|live`,
`blender render`, `studio publish|open`, `mcp`, `migrate`, `doctor`.

## 16. Consumer repositories

- A repository adds `.harness/project.toml` (project id, enabled and trusted plugins, backend default,
  score paths, Claude hook settings) and per-app `.harness/app.toml`, `app.py` and `flows/`.
- Repo plugins in `.harness/plugins/<name>/` carry what is specific to the repository: backends and their
  seeds, backend choice policies (local versus a shared environment by branch changes), credentials and
  logins (driver extensions for web and mobile), app helpers, fakes, renderers and UI modules.
- React Native apps embed `@eks-harness/react-native` with adapters for their navigation, store, logout and
  alerts and register their fakes through its API.

## 17. Networking

- `server.publicUrl` is the app host (UI, API, MCP HTTP); `server.shareUrl` optionally names a separate
  host that serves only public share routes (the hub routes by Host header).
- Tunnels (for example cloudflared) run from a service with the token in a file or the environment, never on
  the command line.
- The hub binds to a LAN address for nodes and to localhost; host allowlist, trusted proxies and proxy client
  IP handling stay.

## 18. Quality

- Python: pytest with fake pools/drivers/nodes; ported test suites from the
  harness and video engine; new tests for
  plugins, nodes, scores (fake clock and fake device), migrations.
- UI and SDKs: vitest and typed builds; screenshot review per section 13.
- Golden-frame checks for render paths that must be frame-exact (score takes,
  web scene virtual clock, Blender event keys).

## 19. Migrating an existing installation

1. Build and test the new system next to the running one with an isolated `EKS_HARNESS_HOME`.
2. Back up the data and config directories.
3. Stop the old service, install the new one (`eks-harness daemon install`, which retires the labels in
   `service.retireLabels`), merge settings, verify users, keys, share URLs and devices.
4. Register nodes (`eks-harness migrate farm --apply`) and verify a render on them.
5. Switch consumer repositories to their repo plugins and the Claude plugin; remove their old harness pieces.
Rollback: keep the old service definition and the backup until the new system has run cleanly for a while.

## 20. Further suggestions (not committed scope)

- Visual regression: screenshot baselines per flow with pixel/structure diffs
  in the artifact viewer.
- Flow recorder: record a manual session in the UI into a flow file.
- Step-synced playback: scrub a recording with its step log, network and
  console aligned.
- Scheduled flows (smoke checks against staging) with history.
- Secrets vault for backend and login credentials (declined for now; plugins
  read credentials from user config).
