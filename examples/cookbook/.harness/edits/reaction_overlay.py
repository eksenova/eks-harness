import eks
from eks import t


@eks.edit(fps=30, size=(1080, 1920), seed=9, duration=t("6s"))
def reaction_overlay(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    main = e.track("main")
    main.clip(e.media("../assets/main_clip.mp4"), start=t("0s"), src=t.span(t("0s"), t("6s"))).effect(
        eks.fx.lower_third("Ayşe Yılmaz", "Harman Kuyumculuk"),
        eks.fx.watermark("../assets/sticker.png", x=900, y=60, opacity=0.8, scale=0.5))
    pip = e.track("reaction", z=10)
    pip.clip(e.media("../assets/reaction_clip.mp4"), start=t("1s"), src=t.span(t("0s"), t("5s")), fit="contain").set(
        scale=0.35, x=300.0, y=-620.0).transition_in(eks.tx.iris(t("400ms")))
    e.expect.derive(beats=False, loudness=False)
