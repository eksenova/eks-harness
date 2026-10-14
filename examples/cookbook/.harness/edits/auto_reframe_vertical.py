import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=43)
def auto_reframe_vertical(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.track("main").clip(e.media("../assets/interview.mp4"), start=t("0s"), src=t.span(t("0s"), t("6s")),
                         fit="contain", audio=True).effect(eks.fx.auto_reframe(target_aspect=(9, 16), tracker="face"))
    e.expect.derive(beats=False)
