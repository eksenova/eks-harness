import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=11)
def beat_flash_montage(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.audio.track("../assets/music.wav", name="music", gain=-6).beats("beat", "downbeat")
    main = e.track("main")
    clips = [e.media(f"../assets/clip_{index}.mp4") for index in range(4)]
    for index, clip in enumerate(clips):
        piece = main.clip(clip, start=t(f"beat:{index * 4}"), src=t.span(t("0s"), t("2s")))
        if index:
            piece.transition_in(eks.tx.beat_flash(t("8f"), stream="beat"))
    main.clip(clips[0], start=t("beat:16"), src=t.span(t("2s"), t("3s"))).effect(
        eks.fx.flash(t.beats("downbeat")[4:6], t("3f")))
    e.expect.derive()
