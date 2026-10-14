import eks


@eks.seed("reference-base", backend="reference-api", now="2026-01-15T09:00:00+03:00", seed=2026,
          description="Settings and the Acme company with its admin")
def reference_base(d: eks.Data):
    d.setting("currency", value="TRY")
    acme = d.account("acme", name="Acme Kuyumculuk")
    acme.user("acme-admin", role="admin")


@eks.seed("reference-sample", backend="reference-api", now="2026-01-15T09:00:00+03:00", seed=2026,
          description="Extends reference-base with a branch, a clerk, payments and a note")
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
