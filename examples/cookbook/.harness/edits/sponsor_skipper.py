import eks
from eks import t


@eks.edit(fps=30, size=(1280, 720), seed=61)
def sponsor_skipper(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    talk = e.media("../assets/talk.mp4")
    eks.media.markers(talk, eks.media.SilenceMarkers(name="gaps", threshold_db=-35.0, min_duration=t("400ms")))
    main = e.track("main")
    main.clip(talk, start=t("0s"), src=t.span(t("0s"), t("marker:gaps#0")), audio=True)
    main.clip(talk, start=t("marker:gaps#0"), src=t.span(t("marker:gaps#1"), t("12s")), audio=True).transition_in(
        eks.tx.crossfade(t("6f")))
    e.expect.derive(beats=False, loudness=False)
