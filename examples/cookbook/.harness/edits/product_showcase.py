import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=5, duration=t("8.5s"))
def product_showcase(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.audio.track("../assets/music.wav", name="bed", gain=-10).fade(fade_in=t("300ms"), fade_out=t("800ms"))
    main = e.track("main")
    for index in range(4):
        photo = e.media(f"../assets/photo_{index}.png")
        clip = main.clip(photo, start=t(f"{index * 2}s"), src=t.span(t("0s"), t("2.5s")))
        clip.effect(eks.fx.zoom(1.0, 1.15, ease="in_out_sine"), eks.fx.color_grade(lut="../assets/warm_filmic.cube"))
        if index % 2:
            clip.effect(eks.fx.mirror("horizontal"))
        if index:
            clip.transition_in(eks.tx.crossfade(t("500ms")))
    e.audio.master(loudness=-16, true_peak=-1.5)
    e.expect.derive(beats=False)
