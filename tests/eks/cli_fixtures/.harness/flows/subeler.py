import eks
from eks import by, expect


@eks.flow(app="web-app", seed="standard", as_="harman-admin", tags=["subeler"])
def subeler(s: eks.Session, branch: str = "Kadıköy", rows: int = 1):
    branches = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
    chosen = branches.items.find(name=branch).id
    s.ui.nav("/tenant/settings/branches")
    expect(by.text(branch)).visible()
    s.checkpoint("liste")
    s.checkpoint("liste-telefon", viewport="phone")
    s.ui.nav(f"/tenant/invoice/list?branchId={chosen}")
    expect(by.test_id("invoice-row")).count(at_least=rows)
    return {"branches": branches}
