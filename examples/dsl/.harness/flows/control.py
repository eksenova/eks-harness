import eks
from eks import by


@eks.flow(app="web-app", seed="standard")
def control(s: eks.Session):
    with eks.when(s.ui.exists("#empty-state")):
        s.ui.tap("#create")
    with eks.otherwise():
        s.ui.tap(by.test_id("row").first)

    with eks.each(s.api.get("/api/invoices").items[:3]) as inv:
        s.ui.nav(f"/tenant/invoice/{inv.id}")
        s.checkpoint(f"fatura-{inv.number}")

    with eks.parallel():
        s.ui.nav("/tenant/invoice/list")
        s.api.get("/api/invoices")

    rows = s.ui.count("#row")

    @eks.dynamic
    def per_row(n=rows):
        return [s.ui.tap(by.test_id("row").nth(i)) for i in range(n)]
