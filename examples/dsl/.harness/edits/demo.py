import eks
from eks import t


@eks.edit(fps=30, size=(1080, 1920), seed=7)
def demo(e: eks.Edit):
    clip = e.media("artifact:VID123")
    e.audio.track("music/song.ogg", gain=-6).beats("beat", "downbeat")
    v = e.track("main")
    (v.clip(clip, start=t("0s"), src=t.span(t("2s"), t("14s")))
     .effect(eks.fx.zoom(1.0, 1.08, ease="out_cubic"))
     .transition_out(eks.tx.crossfade(t("8f"))))
    e.track("overlays").overlay(eks.overlay.caption("Kontör işlemleri", at=t.span(t("1s"), t("4s"))))
    e.expect.cut(at=t("beat:8"))
