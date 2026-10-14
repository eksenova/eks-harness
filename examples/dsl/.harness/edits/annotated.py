import eks
from eks import by, t


@eks.edit(fps=30, size=(1080, 1920), seed=7)
def annotated(e: eks.Edit):
    recording = e.media("artifact:VID123")
    e.track("main").clip(recording, start=t("0s"), src=t.span(t("0s"), t("12s")))
    e.track("overlays").overlay(
        eks.overlay.title("Yeni fatura", subtitle="Kontör işlemleri", at=t.span(t("0s"), t("2s"))),
        eks.overlay.callout("#total", "Anlık kur", at=t.span(t("2s"), t("5s"))),
        eks.overlay.spotlight(by.test_id("invoice-summary"), "Özet", dim=0.6, at=t.span(t("5s"), t("8s"))),
        eks.overlay.highlight(by.text("Kaydet"), at=t.span(t("8s"), t("10s"))),
        eks.overlay.arrow(by.text("Kaydet"), to="#total", at=t.span(t("8s"), t("10s"))),
        eks.overlay.redact(by.test_id("customer-iban"), at=t.span(t("0s"), t("12s"))),
        eks.overlay.blur(by.rect(40, 1600, 1000, 200), radius=16, at=t.span(t("0s"), t("12s"))),
        eks.overlay.step(by.point(540, 960), 1, at=t.span(t("10s"), t("12s"))),
    )
    e.expect.text("Anlık kur", at=t.span(t("2s"), t("5s")))
