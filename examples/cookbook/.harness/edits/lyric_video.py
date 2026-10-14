import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=21, duration=t("8s"))
def lyric_video(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.audio.track("../assets/music.wav", name="song", gain=-4).beats("beat", "downbeat")
    background = e.media("../assets/clip_2.mp4")
    main = e.track("main")
    main.clip(background, start=t("0s"), src=t.span(t("0s"), t("6s")), speed=0.75).effect(
        eks.fx.brightness(eks.curves.noise(min_value=-0.08, max_value=0.08, hold=t("250ms"))),
        eks.fx.flash(t.beats("downbeat")[0:8], t("2f")))
    words = e.track("words", z=1)
    lines = ["Kuyumcular", "için", "tek", "ekran"]
    for index, word in enumerate(lines):
        words.overlay(eks.overlay.caption(word, at=t.span(t(f"bar:{index}"), t(f"bar:{index}") + t("3b"))))
    e.expect.derive(cuts=False, loudness=False)
