# M3 report: data

Contract: `contracts-m3.md`. Status: done. The exit check passed on 2026-10-14: the sample seed builds, snapshots
and restores deterministically on a temp hub, a seeded flow reads its data as seed personas, and the shared
infrastructure dependency follows its dependent's lifetime.

## What landed

| Area | Where | Notes |
| --- | --- | --- |
| Resource context | `data/context.py` | `ctx.parent`, `ctx.resolve`, `ctx.fields`, `ctx.api` (persona aware), `ctx.backend`, frozen `ctx.now`, `ctx.seed`, `ctx.random`, `ctx.ids`, `ctx.log`; outputs redacted in journals |
| Deterministic ids | `data/ids.py` | TCKN, VKN (same checksum as eksenapi's `TurkishEntityNumber`), TR IBAN, ULID on the frozen clock, UUID, phone, e-mail |
| Builds | `data/build.py` | extends, phases `schema`, `api`, `finalize`, `mongo`, persona collection, replay entries for `mongo` resources |
| Engine | `data/engine.py` | the real seed hook: reuse, restore and replay, build, snapshot, prune, determinism report; seed runs build with journaled nodes and snapshot when they pass |
| Manifests | `data/store.py` | `<state>/seeds/<definition>/<key>.json`, mode 0600 |
| Executors | `ops/data.py` | `data.create`, `data.extend`, `data.base` |
| Scheduler | `engine/scheduler.py` | seed resources run one at a time in phase and program order |
| Seed modes | `engine/model.py`, `cli/run_cmds.py` | `--seed restore|fresh|rebuild|skip` |
| Personas | `engine/resources.py`, `drivers/runtime.py` | seed personas merged into the backend credentials; `AppContext.persona(key)` for app logins |
| `@eks.backend` in the pool | `backends/eks_defs.py`, `backends/runner.py` | definitions from the tree registry; `requires`, `identity` (`tree`, `host`, `global`), `idle_timeout` |
| Seed hooks | `pools/backends.py` `step`, `plugins/sdk.py` | `reset`, `seed_phase`, `snapshot`, `restore`, `migrations`, `drop_snapshot` |
| Dependencies (BL-016) | `pools/base.py` | dependency leases, cycle and depth checks, stop refusal and `--force`, reconcile after a hub restart, `dependencies` and `dependents` in backend records, CLI and API |
| CLI and API | `cli/seed_cmds.py`, `api/routes_seeds.py` | `eks-harness seed snapshots|show|drop`, `/api/seeds` |

## Decisions taken on the way

* Seeds create resources one at a time. The first temp hub run created payments in parallel, and the row ids
  followed thread timing; the determinism report caught it on the next build. The scheduler now orders every data
  node by phase, then program order.
* A seed extends at most one base: restoring a second snapshot would replace the first.
* `restore` (the default) reuses a seed the backend instance already holds (same key and generation), so a shared
  instance is not reset under other leases; `fresh` always restores.
* A diverging rebuild is a warning (`seed.diverged`), not a failure, until backends get an injectable clock
  (phase 2 adds it to eksenapi).
* The snapshot key uses the backend's `migrations` answer instead of its fingerprint when the backend has the hook, so
  code changes that keep the schema reuse the snapshot.
* The Time grammar has no minute unit; the design example now reads `idle_timeout=t("600s")`.

## Exit check

Reference backends in `examples/reference/backend/server.py` and `.harness/plugins/reference/reference_backend.py`:
`reference-api` (JSON API over SQLite with every seed hook) requires `reference-infra` (a global audit store with a
20 s idle timeout). Seeds `reference-base` and `reference-sample` (`.harness/seeds/reference_seeds.py`) cover keyed and
keyless kinds, references, two personas, a base, and every phase.

| Check | Result |
| --- | --- |
| Seed run builds the base and the sample, snapshots both | run `01M4XPQCFJSQ1T9P4BZ302QVNV` |
| Rebuild compares with the stored manifest | `01M4XPTD3CEBH5T3MHN40NYY93`: `deterministic: true` |
| Fresh backend and infra instances build the same seed | `01M4XPYYS94BA5ZBB5DF3VAAFG`: same backend digest `sha256:152ecc...` and outputs digest `sha256:8d5915...` |
| Flow `data_reference.py` with the seed, API reads as `acme-clerk` and `acme-admin` | `01M4XPV9W6H6W90EJE7B17F2E9` (reused), `01M4XPVP3SRXSXJVSTWES5N2DR` (`--seed fresh`, restore and note replay) |
| Infra refuses to stop while the API holds it, keeps its lease through an API restart, stops 20 s after the API's final stop | checked through `/api/backends`; no server process left |
| Hub restart keeps the dependency lease | checked |

## Tests

* `tests/data/test_reference_seed.py`: the real pool with the reference backends (dependency lifetime, force stop,
  reconcile, build, rebuild determinism, reuse, fresh restore with replay, a replaced instance, unknown seed).
* `tests/data/test_data_units.py`, `tests/data/test_seed_routes.py`, `tests/eks/test_backend_requires.py`,
  `tests/engine/test_engine_scheduler.py::test_seed_resources_run_one_at_a_time_by_phase_then_program_order`.
* Pyright and ruff clean on the new modules.

## Left for later

* `eks-harness backend personas` still lists legacy credential providers; seed personas are listed by
  `eks-harness seed show` and reach persona auth and app logins.
* The legacy `seeder` contribution and the `seed_backend` MCP tool stay until M7 replaces the MCP surface.
