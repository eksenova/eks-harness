import eks


@eks.seed("subeler", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
def subeler(d: eks.Data):
    d.extend(base="standard")
    moda = d.tenant("moda", name="Moda Kuyumculuk")
    moda.persona("moda-admin", role="admin")
    moda.invoices(states=["Draft", "Confirmed"], per_state=2)
