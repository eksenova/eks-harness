# Determinism

A plan is meant to give the same result every time it runs against the same inputs. This page is the contract:
what the authoring side fixes when the plan is built, what each executor guarantees when it runs, and where the
guarantees stop today. The rules come from section 6 of the [design](dsl/design.md); the contracts of each
milestone under [dsl/](dsl/) have the details.

## The rules

| Source | Rule |
| --- | --- |
| Plan | Tracing does no IO. The same entry, parameters, registry and plugins give the same plan JSON and the same hash. Node ids come from the source (scope path, call chain, call text), so blank lines, comments and unrelated edits keep them. |
| Clock | The plan's `now` (`clock.now`, from `@eks.seed(now=)` or the seed a flow names) is frozen and passed to every executor that reads time. |
| Randomness | One root seed per plan (`clock.seed`). Every consumer derives its own seed from the root and a path: `derived_seed(root, path)` is the first 8 bytes of `sha256("<root>:<path>")`. Nothing reads an unseeded random source. |
| Ids | Generated identifiers (TCKN, VKN, IBAN, ULID, UUID, phone, e-mail) come from `ctx.ids` in seed resources, seeded by the resource path. |
| Waits | There is no sleep op. Waits are conditions with `Time` timeouts (`wait_for`, `wait_gone`, `wait_url`, `expect.eventually`, `settle`). `ui.settle(strict=True)`, the default in tests, fails with `timeout` when the page does not settle. `s.hold(t)` exists only inside a recording, for pacing. |
| Network | `s.net.cassette(name, mode="replay")` replays recorded traffic for hermetic runs; `s.net.shape(profile)` applies a fixed network profile. |
| Caches | A cache key covers everything that changes the output: the node hash, the content digests of what it reads, executor and runtime versions, the harness and fonts digests, plugin and project code, the run clock, and for machine-dependent nodes the machine facts (encoder, GPU adapter and driver, model and device). |
| Machines | A pinned backend, model, device, engine or encoder that is not available fails the node with `resolution_unavailable`. Nothing falls back silently. |
| Flakiness | `eks-harness run --repeat N` runs the plan N times, compares the journals and reports the nodes whose results differ. |

## Plans and hashes

Plans are hashed with SHA-256 over canonical JSON (sorted keys, no whitespace, UTF-8, finite numbers only). A node
hash covers its op, op and executor versions, its arguments with every Ref replaced by the hash of the node it
reads, the resources it uses, its `after` edges, output type, pin, branch children and the part of the clock it
depends on. Labels, source positions and names are left out, so renaming an entry or adding a label keeps the
hashes. [reference/ir.md](reference/ir.md) lists exactly what each hash covers.

A node's own seed is `derived_seed(root, node hash)` (with a field suffix when one node needs several), so a seed
changes when the node changes and stays when unrelated nodes change.

## What each executor guarantees

### Driver ops (web, iOS, Android)

* Targets match strictly: zero matches wait until the timeout, more than one match fails with the candidates
  listed, unless the target says `nth`, `first` or `last`.
* Actions wait for the app to settle afterwards (network and DOM quiet on web, the bridge idle on React Native)
  unless `settle=False`.
* `check.checkpoint` checks the window since the previous checkpoint of the session, so a console error is reported
  once, by the checkpoint that follows it.
* The session's time zone, locale and permission policy (`grant`, `deny` or `ask`) are applied before launch.
* The plan's `now` is installed as the app's clock. The web worker installs it in every browser context
  (Playwright's clock: `Date` starts at `now` and time keeps moving, timers run normally). The React Native bridge
  gets a `clock` command when the app connects: `Date` is shifted to start at `now`, while `performance.now()` and
  timers are untouched. The worker logs `clock.frozen`, or `clock.unsupported` when the app's SDK predates the
  command. Native code (iOS and Android date pickers, system UI, the status bar) still reads the device clock, and
  so does the backend unless it has an injectable clock; mask those regions or list them as noise.

Driver ops are not cached. Their output depends on the app and the backend, which the plan cannot hash.

### API ops

`api.get` is idempotent and is retried after a retryable failure; `expect.eventually` re-reads it. `api.post`,
`put`, `patch` and `delete` run exactly once. A status outside `expect_status` (any 2xx by default) fails the node.

### Seeds

* Resources run one at a time, by phase (`schema`, `api`, `finalize`, `mongo`) and then in program order, so row ids
  that the backend assigns follow the seed source, not thread timing.
* `ctx.now` is the seed's frozen `now`; `ctx.seed` and `ctx.random` derive from the root seed and the resource
  path; `ctx.ids` gives stable identifiers per resource.
* A snapshot is keyed by the seed plan hash (which covers its bases), the backend definition, its plugin and version,
  and the backend's `migrations` answer (its fingerprint when it has no `migrations` hook). Code changes that keep
  the schema reuse the snapshot.
* Every build of a key that already has a manifest is compared with it: `outputsDigest` (the canonical resource
  outputs) and, when the backend's `snapshot` hook returns one, the backend's content `digest`. Equal digests record
  `deterministic: true`; different ones record `deterministic: false` with the diverging resource paths and journal
  a `seed.diverged` warning. `eks-harness seed show <key>` prints the report.
* A divergence is a warning, not a failure, because backends without an injectable clock stamp rows with the wall
  clock. Local backends get the plan's `now` through an injectable server clock in a later phase.
* `mongo` resources are replayed after every restore with their recorded fields and must give the same output each
  time; a different output is a determinism warning.
* `--seed restore` (the default) reuses a seed the backend already holds, `fresh` restores the snapshot again,
  `rebuild` builds it again and compares, `skip` leaves the backend as it is.

### Media sources and analyses

* `media.source` digests its content (artifacts by their stored SHA-256, files by their bytes, URLs by the
  downloaded bytes). An `artifact:ID@sha256:HEX` reference fails with `content_mismatch` when the artifact changed.
* `media.markers` records the analyser backend and version in its resolution record; speech to text also records
  model, device and compute type. These are part of the cache key, so a different model or device gives a different
  cache entry.

### Edit render (`edit.render`)

* Every ffmpeg call runs with `-fflags +bitexact`, `-flags:v +bitexact`, `-flags:a +bitexact`,
  `-map_metadata -1`, `-map_chapters -1` and an explicit thread count, with no hardware decoding and no creation
  times in the container.
* With a software encoder (`libx264`, `libsvtav1`) and fixed `threads`, the output is byte-identical on the same
  machine and ffmpeg build. The ffmpeg version and the encoder are in the resolution record and the executor's
  runtime version. With `encoder` unset the executor picks `libx264` for a `final` quality render and the
  machine's hardware encoder otherwise, and reports which one it used.
* Every time is resolved once in ticks; frame indices handed to effects are absolute timeline frames.
* Effects take `derived_seed(timeline seed, "<clip id>/effect:<n>")`, seeded transitions
  `derived_seed(timeline seed, "<clip id>/transition")`.
* Text is drawn with strictly resolved fonts: the shipped IBM Plex Sans (400, 600) and IBM Plex Mono (400), or a
  font file. Another weight fails with `font_unavailable`. The fonts digest is part of the cache key.
* Machine-learning effects (`auto_reframe`, `birefnet_remove`, `matanyone_remove`, `sam3_track`, `punch_in`) are
  device-dependent: their inference device, model, weights and backend version are in the cache key.

### Audio mix (`audio.mix`)

The mix is computed in float64 and written as 24-bit PCM WAV with no dither. Reverb uses a seeded network. The
master is normalised to its integrated loudness target (ITU-R BS.1770-4) and limited at its true-peak ceiling with
4x oversampling; the executor reports the measured loudness and true peak. `stems` and `denoise` run inference and
carry device, model, weights and backend version in their resolution.

### Motion graphics (`gfx.render`)

`cpu=True` renders on Skia raster surfaces and is byte-identical wherever the same skia-python version runs; use it
for goldens and visual checks. GPU renders (Skia GL) are keyed by the GPU adapter and driver, and one clip renders
on one machine. Rendering runs in a child process.

### Media checks (`check.media`)

OCR-based checks (`text`, `safe_area`) record the OCR engine and version. Derived text checks are soft, since OCR is
a hint.

### Blender and web scenes

Blender and web scene executors come with the scene and score milestone. The plan side already records what their
keys need: a Blender render on CPU Cycles without a platform pin carries the platform, GPU, EEVEE and Workbench
renders carry the GPU adapter and driver, and web scenes carry the browser.

## Caching

Only pure ops are cached (edit renders, mixes, gfx, media analyses), in the modes `use` (default), `refresh`,
`readonly` and `off` (`eks-harness run --cache`). A cached result is served only when the whole key matches;
superseded entries age out. Seed snapshots have their own store, described above. A node that is machine-dependent but whose
executor reports no machine facts is executed every time and never stored.

## Checking it

* `eks-harness run <file> --repeat 2` reports diverging nodes.
* `eks-harness run <seed file>::<seed> --seed rebuild` rebuilds a seed and records its determinism report.
* The cookbook edits render byte-identical twice with the cache off (`tests/media/test_cookbook.py`; all of them
  with `EKS_COOKBOOK_FULL=1`).
