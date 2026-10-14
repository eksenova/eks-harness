import eks
from eks import by, expect


@eks.flow(app="web-app", seed="standard", as_="harman-admin", tags=["subeler"])
def subeler(s: eks.Session):
    branches = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
    kadikoy = branches.items.find(name="Kadıköy").id
    s.ui.nav("/tenant/settings/branches")
    expect(by.text("Kadıköy")).visible()
    s.checkpoint("liste")
    s.checkpoint("liste-telefon", viewport="phone")
    s.ui.nav(f"/tenant/invoice/list?branchId={kadikoy}")
    expect(by.test_id("invoice-row")).count(at_least=1)
    return {"branches": branches}
