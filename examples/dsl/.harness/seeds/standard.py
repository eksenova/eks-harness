import eks


@eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
def standard(d: eks.Data):
    d.host_persona("host")
    bayi = d.federation("anadolu-bayi", type="Reseller", permissions=["*"],
                        exclude=["Kts.Federations.*"])
    harman = d.tenant("harman", name="Harman Kuyumculuk", reseller=bayi)
    harman.persona("harman-admin", role="admin")
    d.accountant("yildiz", name="Yıldız Mali Müşavirlik").serves(harman)
    kt = harman.bank_account("kuveyt", currency="TRY", opening=250_000)
    kt.transactions(n=120, profile="kuyumcu")
    harman.invoices(states="*", per_state=3)
