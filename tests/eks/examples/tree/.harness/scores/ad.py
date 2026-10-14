import eks
from eks import t

phone_scene = "phone"
titles = "titles"


@eks.score(fps=30, song="music/song.ogg", window=t.span(t("0s"), t("15s")))
def ad(sc: eks.Score):
    take = sc.take(platform="ios", app="mobile-app", seed="standard", as_="harman-admin")
    with take.flow() as s:
        s.ui.tap("#new-invoice", at=t("bar:1"))
        s.ui.fill("#amount", "5", at=t("beat:6"))
        s.overlay.callout("#total", "Anlık kur", at=t("bar:2"))
    phone = sc.scene(phone_scene, screen=take)
    sc.edit.track("scene").clip(phone)
    sc.edit.track("graphics").clip(sc.gfx(titles))
    sc.audio.master(loudness=-14)
