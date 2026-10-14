import eks
from eks import t


@eks.edit(fps=30, size=(640, 360), seed=41, duration=t("10s"), background="#0d1b2a")
def mix_only(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    music = e.audio.track("../assets/music.wav", name="music", gain=-4, pan=-0.2)
    voice = e.audio.track("../assets/vo.wav", name="voice", start=t("1s"), pan=0.1)
    music.duck("voice", amount_db=-10)
    voice.compress(threshold_db=-20, ratio=3, attack=t("10ms"), release=t("120ms"), makeup_db=2)
    e.audio.master(loudness=-14, true_peak=-1)
    e.expect.loudness(-14, true_peak=-1)
