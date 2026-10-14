import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=29, duration=t("8s"))
def podcast_clip(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.audio.track("../assets/music.wav", name="music", gain=-14).beats("beat", "downbeat")
    e.audio.track("../assets/vo.wav", name="voice", gain=-2)
    card = e.gfx("podcast_card")
    e.track("card").clip(card, start=t("0s")).effect(
        eks.fx.rgb_split(eks.keys({t("0s"): 0, t("2s"): 8, t("4s"): 0})),
        eks.fx.brightness(eks.curves.beat_pulse(t.beats("downbeat")[0:4], eks.curves.exp_decay_env(t("120ms")),
                                                intensity=0.15)))
    e.track("titles", z=1).overlay(eks.overlay.title("Bölüm 42", subtitle="Kuyumculukta dijital kasa",
                                                     at=t.span(t("500ms"), t("3s"))))
    e.audio.master(loudness=-16, true_peak=-1)
    e.expect.derive(cuts=False)
