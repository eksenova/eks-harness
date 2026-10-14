import eks
from eks import t


@eks.edit(fps=30, size=(1280, 720), seed=3, duration=t("6s"))
def before_after_split(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    before = e.media("../assets/before.mp4")
    after = e.media("../assets/after.mp4")
    left = e.track("left")
    left.clip(before, start=t("0s"), src=t.span(t("0s"), t("6s")), fit="contain").set(scale=0.5, x=-320.0).effect(
        eks.fx.text("Önce", size=40, x=40, y=40))
    right = e.track("right", z=1)
    right.clip(after, start=t("1s"), src=t.span(t("0s"), t("5s")), fit="contain").set(scale=0.5, x=320.0).effect(
        eks.fx.text("Sonra", size=40, x=40, y=40)).transition_in(eks.tx.wipe(t("500ms"), direction="right"))
    e.expect.derive(beats=False, loudness=False)
