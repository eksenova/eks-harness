import eks
from eks import t
from eks.audio import EqBand


@eks.edit(fps=30, size=(1080, 1920), seed=7)
def mix(e: eks.Edit):
    footage = e.media("artifact:VID123")
    e.track("main").clip(footage, start=t("0s"), src=t.span(t("0s"), t("15s")))
    song = e.audio.track("music/song.ogg", gain=-8).beats("beat", "downbeat")
    song.stems("drums", "bass", "other").duck("voiceover", amount_db=-10, attack=t("50ms"), release=t("400ms"))
    song.fade(fade_in=t("500ms"), fade_out=t("1s"))
    voice = e.audio.track("music/voiceover.ogg", name="voiceover", start=t("bar:1"))
    voice.denoise().eq(EqBand(frequency_hz=120, gain_db=-6, type="high_pass"), EqBand(frequency_hz=3000, gain_db=2))
    voice.compress(threshold_db=-20, ratio=3, attack=t("10ms"), release=t("120ms"), makeup_db=2)
    e.audio.master(loudness=-14, true_peak=-1)
    e.expect.loudness(-14, true_peak=-1)
