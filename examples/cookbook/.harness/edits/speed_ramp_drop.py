import eks
from eks import keys, t


@eks.edit(fps=30, size=(1280, 720), seed=17, duration=t("7s"))
def speed_ramp_drop(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.audio.track("../assets/drop.wav", name="drop", gain=-6).beats("beat", "downbeat")
    footage = e.media("../assets/parkour.mp4")
    main = e.track("main")
    main.clip(footage, start=t("0s"), src=t.span(t("0s"), t("6s"))).effect(
        eks.fx.speed(keys({t("0s"): 1.0, t("2s"): 0.3, t("3s"): 0.3, t("4s"): 2.0}, ease="in_out_cubic")),
        eks.fx.freeze(t("1500ms"), t("500ms")),
        eks.fx.shake(eks.curves.beat_pulse(t.beats("downbeat")[2:6], eks.curves.exp_decay_env(t("150ms")),
                                           intensity=12.0)))
    e.expect.derive(loudness=False)
