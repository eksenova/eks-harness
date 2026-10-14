import eks
from eks import t


@eks.edit(fps=30, size=(1280, 720), seed=67, duration=t("10s"))
def multicam_switching(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    mic = e.audio.track("../assets/music.wav", name="floor", gain=-6)
    mic.markers(eks.media.OnsetMarkers(name="hits", sensitivity=0.6))
    cams = [e.media(f"../assets/clip_{index}.mp4") for index in range(3)]
    main = e.track("main")
    main.clip(cams[0], start=t("0s"), src=t.span(t("0s"), t("6s")))
    for index in range(1, 4):
        main.clip(cams[index % 3], start=t(f"marker:hits#{index * 3}"), src=t.span(t("0s"), t("6s")))
    e.expect.derive(beats=False)
