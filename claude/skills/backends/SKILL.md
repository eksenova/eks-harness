---
name: backends
description: "Run, share, seed and log into backends: shared backend instances held by leases (acquire, renew, release, idle timeout and cleanup), linking short browser or device leases to a backend, backend info (URLs, ports, commit, branch, credentials for lease holders), plugin and script definitions and their lifecycle hooks, seeders and scenarios, personas, backend choice policies. Use whenever an app needs its API running or seeded, or a login persona."
---

# Backends, seeding and personas

A backend instance is shared. Everyone who asks for the same definition and tree (plus `--key` or
`--param` when given) gets the same instance; each holder takes a **backend lease** on it. The first
lease starts it, it stays up while any lease is held, and once the last lease is gone it is stopped and
cleaned up after its idle timeout (15 min by default). A lease expires when it is not renewed within its
ttl (10 min by default), so a crashed session cannot keep a backend alive.

## The workflow

1. Hold the backend once for the task (or rely on a driver link, step 2):

   ```bash
   eks-harness backend acquire --definition api [--backend-tree <path>]   # waits until ready; prints the lease id and URL
   eval "$(eks-harness backend acquire --definition api --shell)"         # EKS_BACKEND_LEASE, EKS_BACKEND_URL, its env
   ```

2. Take driver leases (browser, simulator, emulator) as short as possible and link them to the backend:

   ```bash
   eks-harness flow run <app>/.harness/flows/x-check.py --backend "$EKS_BACKEND_LEASE" --release
   eks-harness lease acquire --kind browser --project acme/web --backend "$EKS_BACKEND_LEASE"
   ```

   `--backend` takes a backend lease id, a backend id or a definition name (started when needed, with
   `--backend-tree`). The driver lease waits until the backend is ready, holds a lease on it for its own
   lifetime and releases it with itself. Many short driver leases reuse one backend without restarting it.

3. Renew a long-held backend lease (`eks-harness backend renew <lease>`, every few minutes while you work)
   and release it promptly when done (`eks-harness backend release <lease>`). Release driver leases even
   sooner (`eks-harness lease release`, or `--release` on a flow run).

```bash
eks-harness backend list                         # instances, generation, leases, idle countdown, URL
eks-harness backend leases                       # who holds what
eks-harness backend info <lease> [--secrets]     # URLs, ports, commit, branch, version; credentials only with --secrets
eks-harness backend logs --id <backend> --process api
eks-harness backend seed --id <backend> --scenario standard
eks-harness backend stop --id <backend> [--cleanup]   # ends every lease on it; ask first when others hold it
eks-harness backend choose --shell              # which backend the branch needs (policy plugins)
eks-harness backend personas --target local
```

MCP: `backend_acquire`, `backend_lease`, `backend_renew`, `backend_release`, `backend_leases`,
`backend_info`, `backend_stop`, `lease_acquire(..., backend=...)`, `lease_release`, `lease_backend`,
`flow_run(..., backend=...)`, `list_backends`, `seed_backend`, `list_personas`.

## Rules

- Never pass credentials around in plain text: read them with `--secrets` (or `app.backend.credentials`
  inside a flow) only where a login needs them, and never paste them into reports, PRs or artifacts.
- `--restart` replaces the instance for every holder; use it only when the backend is broken.
- Seed with seeder scenarios; when a feature needs data the scenario lacks, extend the seeder in the same
  branch instead of creating data by hand.
- Never point a harness at production; `backend choose` explains the choice.

## Writing a backend plugin

`eks-harness plugin new backend <id> .harness/plugins/<name>` scaffolds one. Hooks (all optional except
`describe` and one of `spec` or `start`): `describe()` (ports, `idleTimeout`), `fingerprint(ctx)` (a
change replaces the instance), `prepare(ctx)`, `spec(ctx)` (processes the hub supervises, with an `info`
template such as `BackendInfo(url="http://127.0.0.1:{ports.api}")`) or `start(ctx)` (self-managed, returns
`BackendInfo`), `health(ctx)`, `info(ctx)` (`BackendInfo`: url, urls, ports, credentials, env,
secret_env, commit, branch, version, worktree, extra; wrap secrets in `Secret(...)`), `stop(ctx)`,
`cleanup(ctx)` (drop the instance's data), `seed(ctx, scenario)`. Older plugins with `teardown(ctx,
final)` keep working. Set the idle timeout with `idle_timeout` on `[[contributes.backend]]` or
`[backends] idle_timeout` in `.harness/project.toml`.
