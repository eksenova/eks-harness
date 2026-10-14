import eks
from eks import t


@eks.scene(kind="gfx", size=(1080, 1920))
def titles(sc: eks.GfxScene):
    title = sc.text("EksenGOLD", id="title", y=-600.0, size=96.0, color="#f5c542")
    sc.text("Anlık kur", id="subtitle", y=-480.0, size=56.0, start=t("bar:1"))
    sc.on(t.beats("downbeat")[0:8], title.pulse(amount=1.06))
