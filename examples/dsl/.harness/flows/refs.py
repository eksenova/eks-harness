import eks
from eks import by, expect


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def refs(s: eks.Session):
    invoices = s.api.get("/api/invoices", query={"MaxResultCount": 20})
    drafts = invoices.items.filter(state="Draft")
    newest = invoices.items.first
    oldest = invoices.items.last
    kadikoy = invoices.items.find(branch="Kadıköy")
    s.ui.nav(f"/tenant/invoice/{newest.id}")
    expect(by.test_id("invoice-number")).text(newest.number)
    expect(drafts.len()).ge(0)
    expect(invoices["totalCount"]).gt(0)
    s.checkpoint(f"fatura-{oldest.number}")
    return {"drafts": drafts, "kadikoy": kadikoy.id, "first_three": invoices.items[:3]}
