---
name: backends
description: "Run, share and inspect backends: shared backend instances held by leases (acquire, renew, release, idle timeout and cleanup), how eks runs choose and hold a backend (seed=, backend=, as_=, --backend, app.toml [backend], backend policies), backend info (URLs, ports, commit, branch, credentials for lease holders), logs, @eks.backend definitions with dependencies and identities, and backend choice policies. Use whenever an app needs its API running, a run waits for its backend, or a backend misbehaves."
---

# Backends

A backend instance is shared. Everyone who asks for the same definition and identity (the tree by default,
plus `--key` or params) gets the same instance; each holder takes a **backend lease** on it. The first lease
starts it, it stays up while any lease is held, and once the last lease is gone it is stopped and cleaned up
after its idle timeout. A lease expires when it is not renewed within its ttl, so a crashed session cannot
keep a backend alive.

## How a run gets its backend

A flow holds a backend only when it needs one: it names `seed=`, `backend=` or `as_=`, or uses an op that
needs one (`s.api.*`, `s.as_`, `s.backend.info`). A UI-only flow runs without a backend. The definition is
chosen, first match wins: `--backend` on the run, `backend=` on the flow or its seed's backend, the app
profile's `[backend]` table, an `@eks.backend_policy`, a legacy policy plugin, the project's `[backends]
default`. `eks-harness backend choose` shows what a tree gets and why.

The run takes its own lease on the instance, restores the seed into it (skill `eks-harness:eks-data`),
renews the lease while it runs and releases it at the end. The chosen backend's identity (definition,
plugin version, fingerprint, commit) is in the report's environment and keys the seed snapshot and
cassettes.

## Holding one across runs

```bash
eval "$(eks-harness backend acquire --definition api --shell)"   # EKS_BACKEND_LEASE, EKS_BACKEND_URL
eks-harness run flows/a.py --backend "$EKS_BACKEND_LEASE"
eks-harness run flows/b.py --backend "$EKS_BACKEND_LEASE"
eks-harness backend renew "$EKS_BACKEND_LEASE"                    # every few minutes while you work
eks-harness backend release "$EKS_BACKEND_LEASE"                  # when done
```

`--backend` takes a backend lease id, a backend id or a definition name (`--backend-tree`, `--backend-key`
for another tree or identity). Runs with the same lease reuse one instance without restarting it.

```bash
eks-harness backend list                         # instances, generation, leases, dependencies, idle countdown, URL
eks-harness backend leases                       # who holds what
eks-harness backend info <lease> [--secrets]     # URLs, ports, commit, branch, version; credentials only with --secrets
eks-harness backend logs --id <backend> --process api
eks-harness backend definitions                  # definitions found for this tree
eks-harness backend stop --id <backend> [--cleanup] [--force]   # ends every lease on it; ask first when others hold it
eks-harness backend restart --id <backend>       # replaces it for every holder; only when it is broken
```

MCP: `backend_acquire`, `backend_lease`, `backend_renew`, `backend_release`, `backend_leases`,
`backend_info`, `backend_stop`, `choose_backend`, `list_backends`, `list_backend_definitions`, and
`eks_run(..., backend=...)`.

## Rules

- Never pass credentials around in plain text: read them with `--secrets` only where a login needs them,
  and never paste them into reports, PRs or artifacts. Flows log in through personas, not passwords.
- Never point a harness at production; `backend choose` explains the choice.
- Data comes from seeds; when a feature needs data the seed lacks, extend the seed in the same branch.
- Stopping a dependency (an infrastructure backend) is refused while dependents run, unless `--force`,
  which stops the dependents first.

## Defining a backend

```python
import eks
from eks import t
from eks_harness.plugins.sdk import BackendInfo, BackendSpec, Health, ProcessSpec, Secret


@eks.backend("reference-api", requires=["reference-infra"], description="Reference JSON API over SQLite")
class ReferenceApi:
    def describe(self): return {"description": "reference API", "ports": ["api"]}
    def fingerprint(self, context): return server_digest()
    def prepare(self, context): ...
    def spec(self, context) -> BackendSpec:
        port = int(context["ports"]["api"])
        url = f"http://127.0.0.1:{port}"
        infra = context["dependencies"]["reference-infra"]["url"]
        return BackendSpec(
            processes=[ProcessSpec(name="api", port="api", command=[...], health=Health(url=f"{url}/health"))],
            info=BackendInfo(url=url, credentials={"admin": {"headers": {"X-Admin-Key": Secret("...")}}}))
    def teardown(self, context, final=False): return None
```

- `@eks.backend(name, requires=(), identity="tree"|"host"|"global", idle_timeout=t("600s"))`. Dependencies
  start first and stay leased by the dependent for its whole life; `context["dependencies"][name]` carries
  their info. Infrastructure (databases, object stores) is usually `identity="global"` so every tree shares it.
- Hooks: `describe`, `fingerprint` (a change replaces the instance), `prepare`, `spec` (supervised processes)
  or `start` (self-managed), `health`, `info`, `stop`, `cleanup`, `teardown`, `persona_headers`, and the seed
  hooks (`reset`, `seed_phase`, `snapshot`, `restore`, `migrations`, `drop_snapshot`).
- `@eks.backend_policy(name)` classes implement `choose(tree, requested) -> BackendChoice(backend=, reason=)`.
- A complete example with an infrastructure dependency is
  `examples/reference/.harness/plugins/reference/reference_backend.py` in the eks-harness repository.
