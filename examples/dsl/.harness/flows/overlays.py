import eks
from eks import by, t


@eks.flow(app="web-app", seed="standard", as_="harman-admin")
def overlays(s: eks.Session):
    s.ui.nav("/tenant/invoice/new")
    with s.record("fatura-anlatim", pace="demo", pointer=True):
        s.overlay.title("Yeni fatura", subtitle="Kontör işlemleri", hold=t("2s"))
        s.overlay.caption("Tutarı girin", position="bottom", step=1, hold=t("2s"))
        s.overlay.step("#amount", 1, hold=t("2s"))
        s.ui.fill("#amount", "2450.10")
        s.overlay.callout("#total", "Anlık kur", placement="top", hold=t("2s"))
        s.overlay.spotlight(by.test_id("invoice-summary"), "Özet", dim=0.6, hold=t("2s"))
        s.overlay.highlight("#submit", shape="rounded", hold=t("1s"))
        s.overlay.arrow(by.text("Kaydet"), to="#total", text="Toplam burada", hold=t("1s"))
        s.overlay.blur(by.test_id("customer-tckn"), radius=12, keep=True, key="tckn")
        s.overlay.redact(by.test_id("customer-iban"), keep=True, key="iban")
        s.overlay.icon("#submit", "check", hold=t("1s"))
        s.overlay.image(by.test_id("logo"), "assets/eksengold-logo.png", width=120, hold=t("1s"))
        s.ui.tap("#submit")
        s.overlay.remove("tckn")
        s.overlay.clear()

    shot = s.shot("fatura-ozet", full=True)
    annotated = eks.annotate(shot, [
        eks.overlay.callout("#total", "Anlık kur"),
        eks.overlay.highlight(by.test_id("invoice-summary")),
        eks.overlay.redact(by.test_id("customer-iban")),
        eks.overlay.arrow(by.text("Kaydet"), to="#total"),
        eks.overlay.step("#amount", 1),
    ], style="review")
    return {"annotated": annotated}
