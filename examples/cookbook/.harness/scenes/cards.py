import eks
from eks import t


@eks.scene(kind="gfx", size=(720, 1280), fps=30, duration=t("6s"), cpu=True)
def title_card(sc: eks.GfxScene):
    sc.rect(id="panel", width=640.0, height=360.0, radius=36.0, fill="#0d1b2a", y=-120.0)
    title = sc.text("EksenGOLD", id="title", y=-160.0, size=72.0, color="#f5c542")
    sc.text("Anlık kur", id="subtitle", y=-70.0, size=40.0, start=t("1s"))
    sc.image("../assets/logo.svg", id="logo", y=220.0, width=200.0, height=200.0)
    title.animate("opacity", {t("0s"): 0.0, t("600ms"): 1.0}, ease="out_cubic")
    sc.on(t.set(t("2s"), t("3s"), t("4s")), title.pulse(amount=1.08, duration=t("300ms")))


@eks.scene(kind="gfx", size=(720, 1280), fps=30, duration=t("8s"), cpu=True, bpm=120.0)
def podcast_card(sc: eks.GfxScene):
    sc.rect(id="bg", width=720.0, height=1280.0, fill="#1b263b")
    ring = sc.ellipse(id="ring", width=360.0, height=360.0, stroke="#f5c542", stroke_width=16.0)
    sc.text("Bölüm 42", id="episode", y=320.0, size=56.0, color="#e0e1dd")
    sc.on(t.beats("beat")[0:16], ring.pulse(amount=1.1))
