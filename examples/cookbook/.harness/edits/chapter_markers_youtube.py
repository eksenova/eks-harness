import eks
from eks import t


@eks.edit(fps=30, size=(1280, 720), seed=59, duration=t("12s"))
def chapter_markers_youtube(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    talk = e.media("../assets/talk.mp4")
    eks.media.markers(talk, eks.media.SceneMarkers(name="chapters", threshold=20.0))
    e.track("main").clip(talk, start=t("0s"), src=t.span(t("0s"), t("12s")), audio=True)
    titles = e.track("titles", z=1)
    for index, label in enumerate(["Giriş", "Kurulum", "Kullanım"]):
        titles.overlay(eks.overlay.caption(label, at=t.span(t(f"marker:chapters#{index}"),
                                                            t(f"marker:chapters#{index}") + t("2s"))))
    e.expect.derive(beats=False, captions=False)
