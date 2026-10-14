import eks
from eks import t


@eks.edit(fps=30, size=(1080, 1920), seed=7)
def extending(e: eks.Edit):
    clip = e.media("artifact:VID123")
    main = e.track("main")
    (main.clip(clip, start=t("0s"), src=t.span(t("0s"), t("6s")), opacity=eks.curves.custom("heartbeat", bpm=90.0))
     .effect(eks.fx.custom("sparkle", density=2.0), eks.fx.zoom(1.0, 1.1, ease="out_soft"))
     .transition_out(eks.tx.custom("gold_wipe")))
    main.clip(clip, start=t("6s"), src=t.span(t("6s"), t("12s"))).effect(eks.fx.custom("sparkle", density=0.5))
    e.track("badges").overlay(eks.overlay.custom("badge", anchor="#total", text="Yeni", at=t.span(t("1s"), t("3s"))))
