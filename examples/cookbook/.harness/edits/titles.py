import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=31)
def titles(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.track("background").clip(e.media("../assets/clip_1.mp4"), start=t("0s"), src=t.span(t("0s"), t("6s"))).effect(
        eks.fx.blur(6.0), eks.fx.vignette())
    e.track("card", z=1).clip(e.gfx("title_card"), start=t("0s")).transition_out(eks.tx.crossfade(t("500ms")))
    e.expect.derive(beats=False, loudness=False)
