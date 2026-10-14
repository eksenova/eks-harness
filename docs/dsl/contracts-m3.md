# M3 contracts: data

M3 makes seeds real: resource kinds create their data through a typed context, the engine orders creation by the
resource graph and by phase, snapshots the result keyed by the backend's migrations and the seed plan hash, restores it
into backend leases, and hands the personas the seed created to persona auth and app logins. It also makes
`@eks.backend` definitions runnable by the backend pool and adds backend dependencies (BL-016, design 7.1). It builds
on contracts-m0.md 10.3 (seed authoring) and contracts-m1.md 10 (resource planner and the seed hook).

## 1. Module map

| Module | Owns |
| --- | --- |
| `eks_harness/data/context.py` | `ResourceContext` (the `create(self, ctx)` argument), `SeedApi`, `Ids` |
| `eks_harness/data/ids.py` | deterministic generators: TCKN, VKN, IBAN, ULID, UUID, phone, e-mail |
| `eks_harness/data/build.py` | `SeedBuild`: per backend and run, runs extends and resources, phases, outputs, personas |
| `eks_harness/data/store.py` | `SnapshotStore`: manifests under `<state>/seeds/<definition>/<key>.json`, pruning |
| `eks_harness/data/engine.py` | `SeedEngine`: the real `SeedHook` (restore or build), finish of seed runs, determinism report |
| `eks_harness/data/backend.py` | `BackendSteps`: calls the seed hooks of a backend (`reset`, `seed_phase`, `snapshot`, `restore`, `migrations`, `drop_snapshot`) through the pool |
| `eks_harness/ops/data.py` | executors of `data.create`, `data.extend`, `data.base` |
| `eks_harness/backends/eks_defs.py` | `@eks.backend` definitions as pool definitions (source `eks`) |
| `eks_harness/pools/dependencies.py` | dependency leases, cycle check, reconciliation |
| `eks_harness/cli/seed_cmds.py` | `eks-harness seed snapshots|show|drop` |

## 2. Resource creation

A resource kind is an `eks.Resource` model registered with `@eks.resource`; its `create(self, ctx)` method creates the
resource and returns its output (a JSON object, or `None` for `{}`). `self` is the model validated from the node's
unresolved fields, so reference fields hold their `RefExpr`; their values come from `ctx.resolve`.

`ResourceContext`:

| Member | Meaning |
| --- | --- |
| `kind`, `key`, `path`, `phase` | of this resource |
| `parent` | output of the parent resource (`None` for a root kind) |
| `resolve(value)` | output of a referenced resource: takes a field value of `self` (a `RefExpr` or a handle expression) and returns the resolved value; anything else is returned as is |
| `fields` | the resolved fields (references replaced by outputs) |
| `api` | `SeedApi` on the backend URL: `get/post/put/patch/delete(path, json=, query=, headers=, as_=)` return the parsed body; a status outside 2xx (or `expect=`) raises `SeedError` with method, URL, status and a body excerpt; `as_` authenticates as a persona this seed or its bases created, or as a backend persona |
| `backend` | `BackendInfo` of the backend (revealed: url, credentials, env, extra) |
| `now` | the seed's frozen `now` as an aware `datetime` |
| `seed` | `derived_seed(root seed, path)` |
| `random` | `random.Random(seed)` |
| `ids` | `Ids(seed)`: `tckn()`, `vkn()`, `iban(bank_code="00205")` (TR, valid mod 97), `ulid()` (time = `now`), `uuid()`, `phone()` (`+905...`), `email(local=None, domain="example.test")`; each call advances a counter, so the n-th call of a resource is stable |
| `log(message)` | a line in the run journal (seed runs) or the seed resource record (builds inside a flow's acquisition) |

Rules:

* The output must be JSON (`dict`); a non-JSON value fails the resource (`seed_error`).
* A persona kind (`persona=True`) returns the persona's credentials in one of the forms persona auth reads
  (`headers`, `token`, `username` and `password`, `api_key` with `header`), plus anything the app login needs
  (`tenant`, `roles`, `label`). The engine records them as the seed's personas under the resource key.
* Outputs are journaled with the values of keys named `password`, `token`, `secret`, `api_key`, `apiKey`, `headers`
  and `authorization` replaced by `<redacted>`; manifests keep them (mode 0600).
* A kind without `create` fails at run time with `seed_error` naming the kind (M0 allowed it at plan time).

## 3. Order and phases

Phases run in the order `schema`, `api`, `finalize`, `mongo`. Inside a phase the order is the plan's program order,
which respects the resource graph (parents and referenced resources are declared first).

* Every `data.extend` runs before any `data.create` or `data.base` (M1 rule); a `data.create` of phase P waits until
  every `data.create` of an earlier phase settled (new scheduler rule `_phase_blocked`).
* Before the first resource of a phase runs in a build, the engine calls the backend's `seed_phase(context, phase)`
  hook once (optional; eksenova stops the API for `schema` and starts it for `api`).
* `mongo` resources are those whose data lives outside the snapshot. They run after the other phases in a build and
  again after every restore, in recorded order, with the recorded resolved fields and parent outputs. Their outputs
  must be the same each time; a different output is a determinism warning (section 7).

## 4. Backend seed hooks

Optional methods of a backend implementation (legacy plugin backends and `@eks.backend`), called in the backend's own
process through the pool runner (`BackendSteps`), each with the backend context:

| Hook | Contract |
| --- | --- |
| `reset(context)` | bring the store to its empty, migrated state |
| `seed_phase(context, phase)` | prepare for a phase |
| `snapshot(context, name)` | save the current state under `name`; may return `{"digest": "sha256:..."}`, a digest of the logical content |
| `restore(context, name)` | load the state saved under `name` |
| `migrations(context)` | a text that changes exactly when the schema changes |
| `drop_snapshot(context, name)` | delete a saved state |

`pools.backends.step(backend_id, step, **kwargs)` runs one hook and returns its JSON answer; a backend that does not
implement the hook answers `{"missing": true}`. A build needs `reset`; without it the engine refuses to build into a
backend that already holds data (it builds only into a fresh generation). Without `snapshot` and `restore` every
restore is a build and the resource record says so.

## 5. Keys and manifests

* Snapshot key: `seed_key(seed_hash, identity)` from M1, where the identity's fingerprint is replaced by
  `migrations:<answer>` when the backend implements `migrations`. So a key changes with the seed plan (which covers its
  bases through `data.extend` hashes), the backend definition, its plugin and version, and the schema.
* Manifest `<state>/seeds/<definition>/<key hex>.json` (mode 0600), schema `eks/seed-snapshot@1`: `key`, `seed`,
  `seedHash`, `definition`, `migrations`, `snapshot` (the name given to the backend, `seed-<key hex[:16]>`), `digest`
  (backend digest or null), `outputsDigest` (digest of canonical outputs), `outputs` `{path: output}`, `personas`
  `{key: output}`, `replay` (mongo resources: path, kind, key, fields, resolved, parent), `bases` (keys), `created`,
  `buildMs`, `resources`, `runs` (run ids that built it), `deterministic` (null, true or false) and `divergence`.
* Pruning: after a snapshot, manifests of the same definition and seed beyond `seeds.keepSnapshots` (default 3,
  newest kept) are dropped with `drop_snapshot`.

## 6. Restore and build

`SeedEngine.restore(request, info)` (registered for `"*"`), under a lock per backend id:

1. `rebuild` mode, or no manifest for the key: build (below).
2. `restore` mode when the backend record's `seed` equals `{key, generation}`: nothing to do (`restored=False`,
   `reused=True`), the instance already holds the seed. Other leases of a shared instance keep their data.
3. Otherwise (`fresh` mode, another seed, or another generation): `restore(name)`, replay `mongo` resources, record
   `seed` on the backend record.

Build: `reset`, restore each base (recursively building a missing base), run the phases, snapshot, write the manifest,
record `seed` on the backend record. A failed build leaves no manifest, clears the record's `seed` and fails the
acquisition with the resource path, kind and error.

Seed runs (`plan.kind == "seed"`): the backend resource names the plan's own seed, so acquisition starts a build
(`reset`, `seed_phase`) instead of a restore, and the resources run as plan nodes with real executors and a journal.
`data.extend` restores (or builds) the base; `data.base` returns the base manifest's output for the path. When the
scheduler ends with `passed`, the run manager calls `SeedEngine.finish`, which snapshots, writes the manifest and
returns the determinism report as a journal event `seed.snapshot`; a failure there turns the run into `error`.

Run option `seed`: `restore` (default), `fresh` (always restore), `rebuild` (build even when a manifest exists),
`skip` (M1).

## 7. Determinism report

A build whose key already has a manifest (a rebuild or a seed run) compares `outputsDigest` and, when both are set,
`digest` with the stored ones. Equal: `deterministic: true`. Different: `deterministic: false` with `divergence`
listing the paths whose outputs differ (up to 20) and whether the backend digest differs; the run journals
`seed.diverged` as a warning and the new manifest replaces the old one. A diverging seed does not fail the run: a
backend without an injectable clock (phase 2 adds it to eksenapi) stamps rows with the wall clock.

## 8. Seed personas

After a restore or build, the engine merges the manifest's personas into the backend handle's revealed info
(`credentials.personas[key]`, seed entries winning over backend entries of the same key), so `CredentialsAuth` and
backend `persona_headers` hooks see them; `api.*` with `as_`, `session.as` and app runtime logins use them.
`AppContext.persona(key)` returns the persona entry for app runtimes. `eks-harness seed show <key>` lists personas
without secrets.

## 9. `@eks.backend` in the pool

`@eks.backend(name, *, description="", requires=(), identity="tree", idle_timeout=None)`:

* `requires`: names or `eks.requires(name, key=None, params=None)`.
* `identity`: `tree` (today's `tree:<path>` identity with key and params), `host` (`host:<hostname>`, params ignored)
  or `global` (`global`, params ignored).
* `idle_timeout`: a `Time` duration.

The pool lists these definitions from the tree registry next to script and plugin definitions (script and plugin
names win on a clash, which is reported once). Their steps run in a subprocess, `python -m eks_harness.backends.runner
eks <tree> <name> <step>`, which loads the tree registry and instantiates the class with no arguments. The class
implements the `Backend` or `ManagedBackend` protocol and the optional hooks of section 4.

## 10. Backend dependencies (BL-016)

* A definition's requirements: its own `requires` plus `.harness/project.toml` `[backends.<definition>] requires`
  (names or tables `{name, key, params}`), plus a legacy plugin contribution's `requires`.
* Cycle and depth (8) checks run when a definition is acquired; a cycle names its path.
* Start of a dependent (before `prepare` or `start`): for each requirement the pool acquires a lease of kind
  `dependency` with no expiry, holder `backend <id>`, owner `backend:<id>`, on the dependency instance (its identity
  rules apply), waits until it runs (the backend ready timeout), and passes `context["dependencies"][name]` (the
  revealed info) to every hook of the dependent. The record keeps `dependencies: {name: {backend, lease}}`.
* A replacement (restart, fingerprint change) keeps the leases. A final stop of the dependent (idle, `backend stop`,
  instance end) releases them. A failed dependency fails the dependent's start with the dependency's error.
* With no dependent left, the dependency follows its own idle timeout and `cleanup`.
* `backend stop <id>` refuses while dependents run (`BackendError` naming them) unless `force`, which stops the
  dependents first.
* Hub restart: `reconcile` recreates missing dependency leases of running dependents and releases dependency leases
  whose owner is gone or stopped.
* `public()` adds `dependencies` and `dependents` (backend ids holding a dependency lease); the CLI `backend list` and
  `backend status` print them with the idle countdown (`stopsAt`).

## 11. CLI

`eks-harness seed snapshots [--definition D] [--json]`, `eks-harness seed show <key> [--json]`,
`eks-harness seed drop <key> [--yes]`; `eks-harness run ... --seed restore|fresh|rebuild|skip`;
`eks-harness backend stop <id> --force`.

## 12. Exit check

A sample backend (`examples/reference/backend`, an `@eks.backend` serving a small JSON API over SQLite, with
`reset`, `snapshot` returning a digest of a canonical dump, `restore`, `migrations`, and an `@eks.backend` infra
dependency) and a sample seed with a base seed, keyed and keyless kinds, references, a persona and every phase:

* the seed builds twice into fresh instances with equal `outputsDigest` and `digest` (`deterministic: true`);
* a flow naming the seed restores it and reads data as the seed persona through `s.api.get(..., as_=)`;
* the infra dependency starts with the API, survives an API restart, and stops after its idle timeout once the API
  stops.

Tests cover the same with the in-process pool and a temp hub run.
