# eks-harness documentation

## Start here

| Page | What it covers |
| --- | --- |
| [overview.md](overview.md) | the parts of eks-harness and how they fit: plans and runs, sessions, pools and leases, backends, seeds, media, store, nodes, extending, surfaces |
| [cookbook.md](cookbook.md) | every example under `examples/cookbook`, `examples/reference` and `examples/dsl`: what it shows, how to run it, its source |
| [migration.md](migration.md) | the old flow, driver, seeding, annotation and video APIs mapped to the eks API |

## Reference

Generated from the built-in registry and the CLI parser by `eks-harness docs --out docs/reference`; a test fails when
the checked-in files differ from the generator (`eks-harness docs --check`).

| Page | What it covers |
| --- | --- |
| [reference/ops.md](reference/ops.md) | every op by family: description, Python call, platforms, location, resources, output type, flags, arguments and examples, and the argument types |
| [reference/catalog.md](reference/catalog.md) | effects, transitions, easings, curves, overlay kinds, marker sources and target kinds |
| [reference/checks.md](reference/checks.md) | checkpoint, visual, performance and media checks with their parameters |
| [reference/presets.md](reference/presets.md) | network, pace, overlay style and viewport presets |
| [reference/resources.md](reference/resources.md) | data ops, resource kinds, `create(ctx)` and `ResourceContext`, backend seed hooks, snapshots |
| [reference/ir.md](reference/ir.md) | the plan IR: plans, nodes, resources, clock, sessions, Refs, Time, ids and hashing, with the model field tables |
| [reference/cli.md](reference/cli.md) | every `eks-harness` command, subcommand and option |

The JSON Schema of plans and of each op's arguments comes from `eks-harness schema` (`--out DIR` writes them all,
`--op NAME` prints one).

## Contracts and internals

| Page | What it covers |
| --- | --- |
| [determinism.md](determinism.md) | what a plan fixes and what each executor guarantees: seeds, frozen clock, bitexact ffmpeg, software encoders, gfx CPU and GPU, seed snapshots and their determinism report, cache keys |
| [drivers.md](drivers.md) | the typed op protocol between the engine and the driver workers, targets, the native surface, app runtimes, worker extensions with `defineOp`, process ownership |
| [multi-repo.md](multi-repo.md) | several repositories and worktrees on one hub: project ids, trees, trust, cross-repo plugins, shared backends |
| [updates.md](updates.md) | how eks-harness updates itself from GitHub |
| [video-review.md](video-review.md) | contact sheets and machine checks of recordings and renders |
| [studio-api.md](studio-api.md) | HTTP routes and WebSocket messages of the studio (the legacy video editing surface) |
| [cleanup.md](cleanup.md) | the artifact clean-up page |
| [ui-research.md](ui-research.md) | research behind the web UI design skill |
| [tasks.md](tasks.md) | the original build task tracker |

## Design record

[dsl/](dsl/) holds the planning record of the eks API: [the design](dsl/design.md), the contracts and decisions
of each milestone and the milestone reports. The pages above describe the system as it is; the design record keeps
how it got there.
