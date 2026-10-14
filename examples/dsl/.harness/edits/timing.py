import eks
from eks import t


@eks.edit(fps=30, size=(1080, 1920), seed=7)
def timing(e: eks.Edit):
    footage = e.media("artifact:VID123")
    e.audio.track("music/song.ogg", gain=-6).beats("beat", "downbeat")
    voice = e.audio.track("music/voiceover.ogg", gain=-3)
    voice.markers(eks.media.STTMarkers(name="vo", language="tr"))
    eks.media.markers(footage, eks.media.SceneMarkers(name="intro"))
    logo = e.cue("logo", t("bar:4"))

    main = e.track("main")
    main.clip(footage, start=t.start, src=t.span(t("0s"), t("15s")))
    main.clip(footage, start=t("beat:8") + t("1b"), src=t.span(t("f:120"), t("f:240")))

    captions = e.track("captions")
    captions.overlay(
        eks.overlay.title("EksenGOLD", at=t.span(t("1.5s"), t("1.5s") + t("300ms"))),
        eks.overlay.caption("Anlık kur", at=t.span(t("bar:4") - t("1b"), t("cue:logo") - t("2f"))),
        eks.overlay.caption("Kontör işlemleri", at=t.span(t("word:'EksenGOLD'"), t("marker:intro#2"))),
        eks.overlay.caption("Hemen deneyin", at=t.span(t.end - t("2s"), t.end)),
    )
    main.clip(footage, start=logo).effect(eks.fx.flash(t.beats("downbeat")[0:8], t("2f")))
    e.expect.cut(at=t("marker:intro#2"))
