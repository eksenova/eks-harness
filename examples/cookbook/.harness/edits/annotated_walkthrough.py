import eks
from eks import by, t


@eks.edit(fps=30, size=(1080, 1920), seed=37, duration=t("6s"))
def annotated_walkthrough(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.track("main").clip(e.media("../assets/main_clip.mp4"), start=t("0s"), src=t.span(t("0s"), t("6s")))
    e.track("notes", z=1).overlay(
        eks.overlay.title("Yeni fatura", subtitle="Kontör işlemleri", at=t.span(t("0s"), t("1500ms"))),
        eks.overlay.callout(by.rect(120, 600, 840, 220), "Anlık kur", at=t.span(t("1500ms"), t("3s"))),
        eks.overlay.highlight(by.rect(120, 900, 840, 160), at=t.span(t("3s"), t("4500ms"))),
        eks.overlay.arrow(by.point(540, 1500), to=by.rect(120, 900, 840, 160), text="Kaydet",
                          at=t.span(t("3s"), t("4500ms"))),
        eks.overlay.redact(by.rect(120, 1700, 840, 120), at=t.span(t("0s"), t("6s"))),
        eks.overlay.step(by.point(540, 400), 1, at=t.span(t("4500ms"), t("6s"))),
        eks.overlay.caption("Faturayı kaydedin", at=t.span(t("4500ms"), t("6s"))),
    )
    e.expect.derive(beats=False, loudness=False)
