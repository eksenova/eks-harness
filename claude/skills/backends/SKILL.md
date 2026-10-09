---
name: backends
description: Run, seed and choose backends: backend definition scripts and plugin backends, ports and exports, health and ready checks, seeders and scenarios, credentials and personas, backend choice policies (local versus a shared environment by branch). Use whenever an app needs its API running or seeded, or a login persona.
---

# Backends, seeding and personas

```bash
eks-harness backend definitions
eks-harness backend choose --shell
eks-harness backend ensure --definition api --shell
eks-harness backend seeders
eks-harness backend seed --definition api --scenario standard
eks-harness backend personas --target local
eks-harness backend logs --definition api --process api
eks-harness backend stop --definition api [--final]
```

- A backend is a plugin `backend` contribution (Python class: `describe`, `fingerprint`, `prepare`,
  `spec`, `teardown`, optional `seed`, `snapshot`, `restore`) or a definition script in
  `.harness/backends/`. `spec` returns processes (command, port, health URL or ready log pattern) and
  exports (`API_BASE_URL=http://127.0.0.1:{ports.api}`) that the hub supervises and hands to frontends.
- Seeders (`seeder` contributions) fill a running backend with named scenarios; when a feature needs data
  the scenario lacks, extend the seeder in the same branch instead of creating data by hand.
- Credentials providers list personas per target without exposing secrets; login helpers in the repo
  plugin use them.
- Backend policies decide local versus a shared environment; `backend choose` shows the decision and
  why. Never point a harness at production.
