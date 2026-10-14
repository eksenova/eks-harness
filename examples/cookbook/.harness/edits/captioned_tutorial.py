import eks
from eks import t


@eks.edit(fps=30, size=(1280, 720), seed=47)
def captioned_tutorial(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    recording = e.media("../assets/talk.mp4")
    eks.media.markers(recording, eks.media.STTMarkers(name="speech", language="tr", model="small", device="cpu",
                                                      compute_type="int8"))
    e.track("main").clip(recording, start=t("0s"), src=t.span(t("0s"), t("10s")), audio=True).effect(
        eks.fx.punch_in(zoom=1.15, track_speaker=False),
        eks.fx.captions("speech", style="subtitle", position="bottom"))
    e.expect.derive(beats=False)
