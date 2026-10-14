---
name: eks-extending
description: "Extend eks: custom ops with hub executors or worker implementations (@eks.op, JS defineOp in a driver extension), macros (@eks.macro as s.<namespace>.<name>), seed resource kinds (@eks.resource), checks (@eks.check), effects, transitions, easings, curves and marker sources, media renderers, overlay kinds, target kinds, presets (viewports, network profiles, pace, overlay styles), backends and backend policies, app runtimes (@eks.app), and the harness-plugin.toml manifest with modules, trust and settings. Use for any repo-specific automation or new integration."
---

# Extending eks

Extensions are Python decorators in modules that a plugin manifest lists (or in the project's own entry
files). Everything registered shows up in `eks-harness ops`, the plan validator and the generated
reference, and its plugin's version and content digest go into every plan hash that uses it.

## Plugin manifest

`.harness/plugins/<name>/harness-plugin.toml`:

```toml
[plugin]
id = "cookbook"
name = "DSL cookbook"
version = "1.0.0"
api = "1"
description = "Seed resource kinds and extensions used by the DSL examples"
modules = ["cookbook_resources", "cookbook_extensions"]
```

The plugin folder is on the import path, so `modules` are file names without `.py` (or dotted packages).
Trust a repo plugin with `[plugins] trust = ["cookbook"]` in `.harness/project.toml` or
`eks-harness plugin trust <id>`. More (requirements, settings, driver extensions, UI modules, scaffolding):
`references/plugins.md`.

## Ops

```python
from typing import Any
from pydantic import Field
import eks


def greet(call: Any) -> dict[str, str]:
    return {"greeting": f"{call.args.salutation} {call.args.name}"}


@eks.op("acme", name="greet", out="json", pure=True, executor=greet,
        description="A greeting for a name, computed on the hub")
class Greet(eks.Args):
    name: str = Field(description="who to greet")
    salutation: str = Field(default="Hello", description="the word before the name")
```

- Call it in a flow with `s.acme.greet(name="Ada")`, `s.op("acme.greet", name="Ada")` or typed with
  `s.call(Greet(name="Ada"))`, or alone with `eks-harness do acme.greet name=Ada`.
- `@eks.op(family, name=, out="none"|"json"|"str"|"int"|"bool"|"artifact"|..., location="hub"|"worker"|"node",
  executor=, platforms=, resources=("lease",)|("backend",), pure=, idempotent=, device_dependent=, pinnable=,
  container=, examples=)`. `pure=True` ops are cached by node hash; `idempotent=True` ones may be retried.
- A hub executor receives the execution context (`call.args` is the validated model; also `call.artifacts`,
  `call.seed`, `call.time()`, `call.media(...)`) and returns a JSON value. A container op passes an object
  with `enter(call)` and `exit(call, outcome)`.
- A worker op (`location="worker"`, `resources=("lease",)`, `platforms=("web",)`) is implemented in JS:

```js
// .harness/plugins/acme/web/ops.mjs, listed as a driver extension
export default function extend(kit) {
  kit.defineOp({
    name: 'acme.badge_count',
    platforms: ['web'],
    async run(args, ctx) {
      return await ctx.page.locator('[data-badge]').count()
    },
  })
  return {}
}
```

  `ctx` has `site`, `session`, `deadline`, `outDir`, `file(name)`, `step(entry)`, `settle(opts)`, and `page`
  (web) or `bridge` and `native` (mobile). A thrown error fails the node as `driver_error` with its
  message (Playwright timeouts become `timeout`); the worker validates `args` against the op's schema
  before `run`.

## Macros

```python
from eks import by


@eks.macro(platforms=["web"], description="Opens an invoice draft for a branch and fills the amount")
def new_invoice(s: eks.Session, branch: str, amount: str = "2450.10") -> eks.Ref[None]:
    s.ui.nav("/tenant/invoice/new", params={"branch": branch})
    s.ui.fill("#amount", amount)
    return s.ui.tap(by.role("button", name="Kaydet"))
```

A macro is a reusable sub-plan, called as `s.<namespace>.<name>(...)`: the namespace is the plugin id
(dots and dashes become `_`), `project` for project code, or `namespace=`. Its arguments are recorded, so
the plan shows them. Logins and app navigation belong in macros.

## Everything else

| Decorator | Contributes | Example |
| --- | --- | --- |
| `@eks.resource(kind, parent=, phase=, keyed=, positional=, persona=)` | a seed resource kind | skill `eks-harness:eks-data` |
| `@eks.check(kind, family="checkpoint"\|"media"\|..., platforms=, default=, impl=)` | a check; params model is the class | `Contrast(minimum=4.5)` |
| `@eks.effect`, `@eks.transition`, `@eks.easing`, `@eks.curve`, `@eks.marker` | catalog entries; the class has `kind: Literal["..."]` | `class Sparkle(eks.EffectBase, frozen=True)` |
| `@eks.renderer(media_kind)` | turns a media file into frames | `@eks.renderer("svg")` |
| `@eks.overlay_kind(live=, screenshot=, video=)` | an overlay with its three renderers | `class Badge(eks.OverlayBase, frozen=True)` |
| `@eks.target_kind(kind, surfaces=, shorthand=, resolvers=)` | a target predicate | `chip=Bekleyen` |
| `@eks.preset(name, category=)` | a named value: `eks.Viewport`, `eks.NetProfile`, `eks.Pace`, `eks.OverlayStyle`, `eks.PersonaPreset` | `viewport="cookbook-tablet"` |
| `@eks.backend(name, requires=, identity=, idle_timeout=)` | a backend definition | skill `eks-harness:backends` |
| `@eks.backend_policy(name)` | `choose(tree, requested) -> BackendChoice` | local when the branch changes the API |
| `@eks.app(name)` | an app runtime: `prepare`, `boot`, `login`, `close` | skill `eks-harness:eks-flows`, `references/apps.md` |

Custom catalog entries are used through `eks.fx.custom("sparkle", density=2.0)`, `eks.tx.custom(...)`,
`eks.curves.custom(...)`, `eks.overlay.custom("badge", anchor=..., text=...)` and `by.custom(...)`.
Every extension above has a working example in `examples/dsl/.harness/plugins/cookbook/` of the eks-harness
repository, and `examples/reference/.harness/plugins/reference/` has real app runtimes, backends and resource
kinds.

Rules: plugin checkpoint checks run only where a worker implements them (others report `check_unsupported`);
plugin media checks with `impl=` run on the hub. Keep executors deterministic: take randomness from
`call.seed`, never from the wall clock.
