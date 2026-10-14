import eks
from eks import t


@eks.scene(kind="gfx", size=(1080, 1920), fps=30)
def lower_third(sc: eks.GfxScene):
    with sc.group(id="bar", y=700.0, start=t("bar:1")) as bar:
        plate = sc.rect(960.0, 180.0, id="plate", radius=24.0, fill="#0b1f3a")
        sc.text("Harman Kuyumculuk", id="name", y=-24.0, size=56.0, color="#f5c542")
        sc.text("Kadıköy Şubesi", id="branch", y=36.0, size=40.0)
        sc.path("M -440 70 L 440 70", id="rule", stroke="#f5c542", stroke_width=4.0,
                trim_end=eks.keys({t("bar:1"): 0.0, t("bar:2"): 1.0}, ease="out_cubic"))
    logo = sc.image("assets/logo.png", id="logo", x=-400.0, y=-800.0, width=160.0)
    clip = sc.video("artifact:VID123", id="screen", y=-100.0, width=900.0, src=t.span(t("0s"), t("6s")))
    window = sc.ellipse(900.0, 900.0, id="window", y=-100.0, fill="#ffffff")
    sc.mask(clip, window)
    bar.animate("x", {t("bar:1"): -1080.0, t("bar:1") + t("12f"): 0.0}, ease="out_cubic")
    plate.animate("opacity", {t("bar:1"): 0.0, t("bar:1") + t("6f"): 1.0})
    sc.on(t.beats("downbeat")[0:8], logo.pulse(amount=1.08))
