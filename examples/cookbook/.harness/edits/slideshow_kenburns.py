import eks
from eks import keys, t


@eks.edit(fps=30, size=(1280, 720), seed=13)
def slideshow_kenburns(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.audio.track("../assets/music.wav", name="bed", gain=-12)
    main = e.track("main")
    transitions = [eks.tx.crossfade(t("600ms")), eks.tx.dip_to_black(t("600ms")), eks.tx.luma_wipe(t("600ms"))]
    for index in range(4):
        clip = main.clip(e.media(f"../assets/photo_{index}.png"), start=t(f"{index * 2.5}s"),
                         src=t.span(t("0s"), t("3s")))
        clip.effect(eks.fx.zoom(1.05, 1.2, ease="linear", cx=keys({t.start: 0.3, t.end: 0.7})))
        if index:
            clip.transition_in(transitions[index - 1])
    main.clip(e.media("../assets/photo_0.png"), start=t("10.5s"), src=t.span(t("0s"), t("1.5s"))).transition_out(
        eks.tx.dip_to_black(t("500ms")))
    e.expect.derive(beats=False)
