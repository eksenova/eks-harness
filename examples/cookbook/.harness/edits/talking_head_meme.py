import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=53)
def talking_head_meme(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    head = e.media("../assets/interview.mp4")
    eks.media.markers(head, eks.media.STTMarkers(name="words", language="tr", model="small", device="cpu",
                                                 compute_type="int8"))
    e.track("main").clip(head, start=t("0s"), src=t.span(t("0s"), t("8s")), audio=True).effect(
        eks.fx.captions("words", style="tiktok", word_animation="pop"),
        eks.fx.flash(t("word:#1"), t("4f")))
    e.expect.derive(beats=False)
