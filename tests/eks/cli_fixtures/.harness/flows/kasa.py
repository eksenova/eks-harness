import eks
from eks import by


@eks.flow(app="web-app", seed="standard", as_="harman-kasiyer")
def kasa(s: eks.Session):
    s.ui.nav("/tenant/cash")
    with eks.when(s.ui.exists("#empty-state")):
        s.ui.tap("#create")
    with eks.otherwise():
        s.ui.tap(by.test_id("row").first)
    s.checkpoint("kasa")


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def iade(s: eks.Session):
    with eks.each(s.api.get("/api/invoices").items[:3]) as invoice:
        s.ui.nav(f"/tenant/invoice/{invoice.id}")
        s.checkpoint(f"fatura-{invoice.number}")
