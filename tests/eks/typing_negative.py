# pyright: reportUnnecessaryTypeIgnoreComment=true
# Type-checked only, never imported: every marked line must be a pyright error. If a signature loosens (a Time
# parameter accepting a bare number, a target accepting an int), the ignore comment becomes unnecessary and the
# pyright run in test_public_api.py fails.
from __future__ import annotations

import eks
import eks.scenes
from eks import by, expect, t


def negative_cases(s: eks.Session, e: eks.Edit) -> None:
    s.ui.tap("#save", timeout=5)  # pyright: ignore[reportArgumentType]
    s.ui.tap(42)  # pyright: ignore[reportArgumentType]
    s.ui.nav("/a", at="1s")  # pyright: ignore[reportArgumentType]
    s.ui.long_press("#b", hold=2.0)  # pyright: ignore[reportArgumentType]
    s.hold(1)  # pyright: ignore[reportArgumentType]
    s.ui.text("#a", timeout="2s")  # pyright: ignore[reportArgumentType]
    expect(by.text("Kaydet")).visible(timeout=3)  # pyright: ignore[reportCallIssue]
    with eks.when(True):  # pyright: ignore[reportArgumentType]
        s.ui.back()
    e.track("main").clip(e.media("artifact:VID1"), start=0)  # pyright: ignore[reportArgumentType]
    t.span(0, 15)  # pyright: ignore[reportArgumentType]
    s.mobile.dispatch({"type": object()})  # pyright: ignore[reportArgumentType]
    s.api.post("/x", json=object())  # pyright: ignore[reportArgumentType]
    s.api.get("/x", query={"ids": {1, 2}})  # pyright: ignore[reportArgumentType]
    s.debug.break_("x", when=s.ui.count("#x"))  # pyright: ignore[reportArgumentType]
    s.debug.break_("no-rows", when=s.ui.state("invoices.items").len())  # pyright: ignore[reportArgumentType]
    s.debug.break_("n", when=s.ui.text("#n"))  # pyright: ignore[reportArgumentType]


def negative_events(sc: eks.Score, media: eks.Media) -> None:
    take = sc.take(platform="ios", app="mobile-app")
    take.event("reveal", row=object())  # pyright: ignore[reportArgumentType]
    eks.scenes.scene_event(media, "e", k=object())  # pyright: ignore[reportArgumentType]


def positive_events(s: eks.Session, sc: eks.Score, media: eks.Media) -> None:
    s.debug.break_("x", when=s.ui.exists("#x"))
    s.debug.break_("y", when=s.api.get("/api/flags").enabled)
    s.debug.break_("z", when=s.ui.state("invoices.items").first)
    take = sc.take(platform="ios", app="mobile-app")
    take.event("reveal", row=2, tags=["a", "b"], total=s.ui.text("#total"))
    eks.scenes.scene_event(media, "e", k={"step": 1})


class Tenant(eks.Resource):
    name: str


@eks.macro(namespace="typing")
def open_invoice(s: eks.Session, branch: str, amount: str = "1") -> eks.Ref[None]:
    return s.ui.nav(f"/invoice/{branch}/{amount}")


def negative_builders(s: eks.Session, e: eks.Edit, d: eks.Data) -> None:
    s.ui.fill("#amount", s.ui.count("#row"))  # pyright: ignore[reportArgumentType]
    s.ui.nav(s.ui.exists("#row"))  # pyright: ignore[reportArgumentType]
    with eks.each(s.ui.count("#row")):  # pyright: ignore[reportCallIssue, reportArgumentType]
        s.ui.back()
    eks.fx.captions("words", colour="red")  # pyright: ignore[reportCallIssue]
    eks.fx.captions("words", size="big")  # pyright: ignore[reportArgumentType]
    clip = e.track("main").clip(e.media("artifact:VID1"))
    clip.set(opacty=0.5)  # pyright: ignore[reportCallIssue]
    clip.set(start=2)  # pyright: ignore[reportArgumentType]
    e.render_settings(qualty="final")  # pyright: ignore[reportCallIssue]
    e.render_settings(quality="best")  # pyright: ignore[reportArgumentType]
    e.audio.track("music/song.ogg").set(gian=-6.0)  # pyright: ignore[reportCallIssue]
    s.macro(open_invoice)(42)  # pyright: ignore[reportArgumentType]
    s.macro(open_invoice)("kadikoy", total="5")  # pyright: ignore[reportCallIssue]
    d.add(Tenant(nme="Harman"), key="harman")  # pyright: ignore[reportCallIssue]
    d.add("tenant", key="harman")  # pyright: ignore[reportArgumentType]


def not_a_backend() -> int:
    return 1


eks.backend("weird")(not_a_backend)  # pyright: ignore[reportArgumentType]


class NotAPolicy:
    def pick(self) -> str:
        return "local"


eks.backend_policy("weird_policy")(NotAPolicy)  # pyright: ignore[reportArgumentType]


def positive_builders(s: eks.Session, e: eks.Edit, d: eks.Data) -> None:
    s.ui.fill("#amount", s.ui.text("#total"))
    s.ui.fill("#amount", s.api.get("/api/rates").items[0].rate)
    nav: eks.Ref[None] = s.macro(open_invoice)("kadikoy", amount="5")
    del nav
    e.track("main").clip(e.media("artifact:VID1")).set(opacity=0.5, start=t("1s"), fit="contain")
    e.render_settings(quality="draft", crf=18)
    e.audio.track("music/song.ogg").set(gain=-6.0, mute=False)
    eks.fx.captions("words", size=72, position="top")
    harman = d.add(Tenant(name="Harman"), key="harman")
    del harman


def negative_scenes(blender: eks.BlenderScene, gfx: eks.GfxScene) -> None:
    phone = blender.empty()
    phone.animate("location", {t("0s"): {1, 2, 3}})  # pyright: ignore[reportArgumentType]
    phone.assign("lens", object())  # pyright: ignore[reportArgumentType]
    phone.set(scale={1, 2})  # pyright: ignore[reportArgumentType]
    card = gfx.rect(10.0, 10.0)
    card.animate("opacity", {t("0s"): {0.0}})  # pyright: ignore[reportArgumentType]
    card.tween("opacity", {t("0s"): object()})  # pyright: ignore[reportArgumentType]
    card.assign("opacity", {0.5})  # pyright: ignore[reportArgumentType]
    card.set(opacity=object())  # pyright: ignore[reportArgumentType]


def positive_cases(s: eks.Session) -> None:
    rows = s.api.get("/api/rows")
    with eks.each(rows.items) as row:
        s.ui.nav(f"/rows/{row.id}")
    with eks.when(s.ui.exists("#empty")):
        s.ui.tap("#create", timeout=t("2s"))
    expect(rows.total).eq({"count": rows.count, "ids": [1, 2]})
    s.mobile.dispatch({"type": "cart/clear", "payload": {"id": rows.id}})
