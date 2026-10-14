import eks
from eks import expect, t


@eks.flow(app="reference-web", seed="reference-sample", timeout=t("10s"))
def data_reference(s: eks.Session):
    me = s.api.get("/api/me", as_="acme-clerk")
    expect(me.role).eq("clerk")
    expect(me.account.name).eq("Acme Kuyumculuk")
    payments = s.api.get("/api/payments", as_="acme-admin")
    expect(payments.items).length(4)
    expect(payments.items[0].amount).eq(1250)
    accounts = s.api.get("/api/accounts", as_="acme-admin")
    expect(accounts.items).length(2)
    notes = s.api.get("/api/notes")
    expect(notes.items).length(1)
    expect(notes.items[0].text).eq("Kasa sayımı cuma günü")
    s.ui.open_url("/")
    return {"payments": payments.items, "me": me}
