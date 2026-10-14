---
name: eks-data
description: "Seed backends with the eks data API: @eks.seed graphs, resource kinds (@eks.resource with create(ctx)), references between resources, phases, personas that come out of the seed, extending a base seed, seed runs, snapshots keyed by migrations and plan hash, restore modes, the backend seed hooks, @eks.backend definitions with dependencies, and how flows use a seed (seed=, as_=, s.api with personas). Use whenever an app needs data in its backend or a login persona."
---

# Data and seeds

A seed is a declarative resource graph for one backend definition. The engine creates the resources in
order, snapshots the backend, and restores that snapshot into every backend lease that names the seed.
Personas are seed outputs, so flows log in as people the seed created.

```python
import eks


@eks.seed("reference-base", backend="reference-api", now="2026-01-15T09:00:00+03:00", seed=2026,
          description="Settings and the Acme company with its admin")
def reference_base(d: eks.Data):
    d.setting("currency", value="TRY")
    acme = d.account("acme", name="Acme Kuyumculuk")
    acme.user("acme-admin", role="admin")


@eks.seed("reference-sample", backend="reference-api", now="2026-01-15T09:00:00+03:00", seed=2026)
def reference_sample(d: eks.Data):
    d.extend("reference-base")
    acme = d.base("account:acme")
    admin = d.base("account:acme/user:acme-admin")
    acme.branch("izmir", name="Acme İzmir")
    clerk = acme.user("acme-clerk", role="clerk")
    for amount in (1250, 4800, 990):
        acme.payment(amount, payer=admin)
    acme.payment(310, payer=clerk)
    acme.note("Kasa sayımı cuma günü")
```

- `d.<kind>(...)` creates a root resource, `handle.<kind>(...)` a child. A keyed kind takes its key first;
  `positional=` names further positional fields; keywords are fields. Unknown or misplaced kinds raise with
  the kinds that fit.
- Paths: `account:acme`, `account:acme/user:acme-admin`, keyless children `payment#0`, `payment#1`. Ids and
  generated values derive from the path, so the same seed builds the same data.
- `d.extend("base")` (once, first) builds on another seed; `d.base("path")` returns a handle to a base
  resource. `d.create(kind, key, parent=, **fields)` and `d.add(Model(...), key=, parent=)` are the generic
  and typed forms. Declare resources in the seed's own scope (Python `for` loops are fine, `eks.each` is not).
- `now=` freezes the seed's clock (and of flows that use it); `seed=` is the root seed.

## Resource kinds

```python
from typing import Annotated, Any
import eks


@eks.resource("account", description="A company account with a generated tax number")
class Account(eks.Resource):
    name: str

    def create(self, ctx: Any) -> dict[str, Any]:
        parent = ctx.parent or {}
        body = {"name": self.name, "taxNumber": ctx.ids.vkn(), "parentId": parent.get("id"),
                "createdAt": ctx.now.isoformat()}
        return dict(ctx.api.post("/api/accounts", json=body))


@eks.resource("user", parent="account", persona=True, description="A user of an account; a seed persona")
class User(eks.Resource):
    role: str = "admin"

    def create(self, ctx: Any) -> dict[str, Any]:
        token = ctx.ids.uuid()
        created = ctx.api.post("/api/users", json={"accountId": ctx.parent["id"], "role": self.role, "token": token})
        return {"id": created["id"], "token": token, "persona": ctx.key, "label": f"{ctx.key} ({self.role})"}


@eks.resource("payment", parent="account", keyed=False, positional=("amount",), phase="finalize")
class Payment(eks.Resource):
    amount: int
    payer: Annotated[eks.ResourceRef, eks.refers_to("user")]

    def create(self, ctx: Any) -> dict[str, Any]:
        payer = ctx.resolve(self.payer)
        return dict(ctx.api.post("/api/payments", json={"accountId": ctx.parent["id"], "amount": self.amount,
                                                       "iban": ctx.ids.iban()}, as_=payer["persona"]))
```

`ctx` (a `ResourceContext`): `kind`, `key`, `path`, `phase`, `parent` (the parent's output), `resolve(ref)`,
`fields`, `api` (`get/post/put/patch/delete(path, json=, query=, headers=, as_=)`; a status outside 2xx
raises with method, URL, status and body excerpt), `backend` (revealed `BackendInfo`), `now` (frozen
`datetime`), `seed`, `random`, `ids` (`tckn()`, `vkn()`, `iban(bank_code="00205")`, `ulid()`, `uuid()`,
`phone()`, `email(local=None, domain="example.test")`), `log(message)`.

- `create` returns a JSON object: the resource's output, which children and references read.
- A persona kind (`persona=True`) returns credentials persona auth understands (`headers`, `token`,
  `username` and `password`, or `api_key` with `header`) plus what the app login needs (`tenant`, `roles`,
  `label`). Secrets are redacted in journals and kept in the snapshot manifest (mode 0600).
- Phases run in order `schema`, `api`, `finalize`, `mongo`; inside a phase, program order. `mongo` resources
  hold data outside the snapshot and run again after every restore.

## Running seeds

```bash
eks-harness plan .harness/seeds/reference_seeds.py::reference_sample   # the resource graph, no hub
eks-harness run .harness/seeds/reference_seeds.py::reference_sample    # build, snapshot, determinism report
eks-harness seed snapshots [--definition reference-api]
eks-harness seed show <key>                                            # resources, personas (no secrets), determinism
eks-harness seed drop <key>
```

MCP: `eks_seeds` lists snapshots, `eks_seed_show` shows one with its personas.

A flow that names a seed restores it into its backend lease before the first node:

```python
import eks
from eks import expect, t


@eks.flow(app="reference-web", seed="reference-sample", timeout=t("10s"))
def data_reference(s: eks.Session):
    me = s.api.get("/api/me", as_="acme-clerk")
    expect(me.role).eq("clerk")
    payments = s.api.get("/api/payments", as_="acme-admin")
    expect(payments.items).length(4)
```

`--seed restore` (default) reuses a backend that already holds the same seed generation, `fresh` always
restores, `rebuild` builds again and compares with the stored snapshot, `skip` uses the backend as it is.
A rebuild that differs is a warning (`seed.diverged`) with the diverging paths, not a failure.

Rules: extend the seed in the same branch when a feature needs data it lacks; never create data by hand
in a shared backend. A persona named by `as_=` is checked at plan time against the seed.

## Backends for seeds

The seed's `backend=` names a backend definition. Seed hooks are optional methods of the backend class:
`reset(context)` (empty, migrated store; required for builds), `seed_phase(context, phase)`,
`snapshot(context, name)` (may return `{"digest": "sha256:..."}`), `restore(context, name)`,
`migrations(context)` (text that changes exactly when the schema changes; it keys the snapshot),
`drop_snapshot(context, name)`. Without `snapshot` and `restore` every restore is a build.

```python
@eks.backend("reference-infra", identity="global", idle_timeout=t("20s"))
class ReferenceInfra: ...

@eks.backend("reference-api", requires=["reference-infra"], description="Reference JSON API over SQLite")
class ReferenceApi:
    def spec(self, context): ...            # processes the hub supervises, BackendInfo
    def reset(self, context): ...
    def snapshot(self, context, name): ...
    def restore(self, context, name): ...
    def migrations(self, context): ...
```

`requires=` names dependencies (`eks.requires(name, key=, params=)` for variants); the hub starts them first,
holds a dependency lease for as long as the dependent lives and passes their info as
`context["dependencies"][name]`. `identity=` is `tree` (default), `host` or `global`. The complete
reference backend is `examples/reference/.harness/plugins/reference/reference_backend.py`; leases, info,
logs and policies are in skill `eks-harness:backends`.

Generated reference: `docs/reference/resources.md` (data ops and resource hooks).
