import eks
from eks import t


@eks.edit(fps=30, size=(1280, 720), seed=23)
def storytelling_bg_music(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    music = e.audio.track("../assets/music.wav", name="music", gain=-6)
    e.audio.track("../assets/vo.wav", name="voice", start=t("500ms"))
    music.duck("voice", amount_db=-12, attack=t("40ms"), release=t("300ms"))
    music.fade(fade_out=t("1s"))
    e.audio.master(loudness=-16, true_peak=-1)
    main = e.track("main")
    main.clip(e.media("../assets/broll_0.mp4"), start=t("0s"), src=t.span(t("0s"), t("5s")))
    main.clip(e.media("../assets/broll_1.mp4"), start=t("5s"), src=t.span(t("0s"), t("5s"))).transition_in(
        eks.tx.whip_pan(t("300ms")))
    e.expect.derive(beats=False)
