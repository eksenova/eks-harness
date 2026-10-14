# Type-checked with pyright (contract section 13): the design examples written against the public `eks` API.
# Not collected by pytest (no test_ prefix); test_public_api.py loads and plans these entries as well.
from __future__ import annotations

from typing import Any

import eks
from eks import by, expect, t


@eks.resource("typing_host_persona", persona=True, description="The host user")
class HostPersona(eks.Resource):
    pass


@eks.resource("typing_federation", description="A reseller federation")
class Federation(eks.Resource):
    type: str = "Reseller"
    permissions: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@eks.resource("typing_tenant", description="A tenant")
class Tenant(eks.Resource):
    name: str = ""


@eks.resource("typing_persona", parent="typing_tenant", persona=True, description="A tenant user")
class TenantPersona(eks.Resource):
    role: str = "admin"


@eks.resource("typing_bank_account", parent="typing_tenant", description="A bank account of a tenant")
class BankAccount(eks.Resource):
    currency: str = "TRY"
    opening: int = 0


@eks.resource("typing_transactions", parent="typing_bank_account", keyed=False, description="Generated transactions")
class Transactions(eks.Resource):
    n: int = 10
    profile: str = "default"


# design 7: a seed with typed, project-defined resource kinds
@eks.seed("typing_standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
def typing_standard(d: eks.Data) -> None:
    d.typing_host_persona("host")
    bayi = d.typing_federation("anadolu-bayi", type="Reseller", permissions=["*"], exclude=["Kts.Federations.*"])
    harman = d.typing_tenant("harman", name="Harman Kuyumculuk")
    harman.typing_persona("harman-admin", role="admin")
    kt = harman.typing_bank_account("kuveyt", currency="TRY", opening=250_000)
    kt.typing_transactions(n=120, profile="kuyumcu")
    assert bayi is not None


# design 4.1: a flow with api reads, refs, targets and expectations
@eks.flow(app="web-app", seed="typing_standard", as_="harman-admin", tags=["subeler"], platforms=["web"],
          timezone="Europe/Istanbul", locale="tr-TR")
def typing_subeler(s: eks.Session) -> dict[str, Any]:
    branches = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
    kadikoy = branches.items.find(name="Kadıköy").id
    s.ui.nav("/tenant/settings/branches")
    expect(by.text("Kadıköy")).visible()
    s.checkpoint("liste")
    s.checkpoint("liste-telefon", viewport="phone")
    s.ui.nav(f"/tenant/invoice/list?branchId={kadikoy}")
    expect(by.test_id("invoice-row")).count(at_least=1)
    return {"branches": branches}


# design 4.3 and 8: control flow, time, network and injection in one flow
@eks.flow(app="web-app", seed="typing_standard", platforms=["web"], timezone="Europe/Istanbul", locale="tr-TR")
def typing_control(s: eks.Session) -> None:
    with eks.when(s.ui.exists("#empty-state")):
        s.ui.tap("#create")
    with eks.otherwise():
        s.ui.tap(by.test_id("row").first)
    with eks.each(s.api.get("/api/invoices").items[:3]) as inv:
        s.ui.nav(f"/tenant/invoice/{inv.id}")
        s.checkpoint(f"fatura-{inv.number}")
    s.net.route("GET", "/api/bank-transactions*").delay(t("800ms"))
    s.net.shape("3g")
    s.inject.script("window.__flags = {beta: true}", when="init")
    s.ui.wait_for(by.role("button", name="Kaydet"), timeout=t("1.5s"))
    expect(by.label("E-posta").within(by.role("dialog"))).visible()
    s.debug.break_("before-submit")


# design 10.2: an edit with clips, effects, transitions, overlays and a review expectation
@eks.edit(fps=30, size=(1080, 1920), seed=7)
def typing_demo(e: eks.Edit) -> None:
    clip = e.media("artifact:VID123")
    e.audio.track("music/song.ogg", gain=-6).beats("beat", "downbeat")
    v = e.track("main")
    (
        v.clip(clip, start=t("0s"), src=t.span(t("2s"), t("14s")))
        .effect(eks.fx.zoom(1.0, 1.08, ease="out_cubic"))
        .transition_out(eks.tx.crossfade(t("8f")))
    )
    e.track("overlays").overlay(eks.overlay.caption("Kontör işlemleri", at=t.span(t("1s"), t("4s"))))
    e.expect.cut(at=t("beat:8"))


# design 10.4: a Blender scene graph with the bpy escape hatch
@eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
def typing_phone(sc: eks.BlenderScene, screen: eks.Media) -> None:
    p = sc.load("assets/phone.blend", obj="Phone")
    p.material("Screen").texture("screen", screen)
    cam = sc.camera(lens=50).look_at(p)
    cam.animate("location", {t("bar:0"): (0, -4, 1), t("bar:4"): (0, -2, 1)}, ease="out_cubic")
    sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

    @sc.bpy
    def rig(bpy: Any, ctx: Any) -> None:
        return None


# the gfx scene design 10.5 uses: two text layers, one pulsing on downbeats
@eks.scene(kind="gfx", fps=30, size=(1080, 1920), seed=3)
def typing_titles(sc: eks.GfxScene) -> None:
    title = sc.text("Anlık kur", id="title", y=-600.0, size=96.0, color="#f5c542")
    sc.text("EksenGOLD", id="brand", y=600.0, size=64.0, color="#ffffff")
    title.animate("opacity", {t("0s"): 0.0, t("0.5s"): 1.0}, ease="out_cubic")
    sc.on(t.beats("downbeat")[0:4], title.pulse(amount=1.1, duration=t("200ms")))


phone_scene = "typing_phone"
titles = "typing_titles"


# design 10.5: a score binding a pinned take, a Blender scene, gfx and the mix to one clock
@eks.score(fps=30, song="music/song.ogg", window=t.span(t("0s"), t("15s")))
def typing_ad(sc: eks.Score) -> None:
    take = sc.take(platform="ios", app="mobile-app", seed="typing_standard", as_="harman-admin",
                   timezone="Europe/Istanbul", locale="tr-TR")
    with take.flow() as s:
        s.ui.tap("#new-invoice", at=t("bar:1"))
        s.ui.fill("#amount", "5", at=t("beat:6"))
        s.overlay.callout("#total", "Anlık kur", at=t("bar:2"))
    phone = sc.scene(phone_scene, screen=take)
    sc.edit.track("scene").clip(phone)
    sc.edit.track("graphics").clip(sc.gfx(titles))
    sc.audio.master(loudness=-14)


def typing_plans() -> list[eks.Plan]:
    return [
        eks.plan(typing_standard),
        eks.plan(typing_subeler),
        eks.plan(typing_control),
        eks.plan(typing_demo),
        eks.plan(typing_phone, screen="artifact:S1"),
        eks.plan(typing_titles),
        eks.plan(typing_ad),
    ]


def typing_time_and_targets() -> tuple[eks.Time, eks.Span, eks.TimeSet, eks.Target]:
    start: eks.Time = t("beat:8") + t("1b")
    window: eks.Span = t.span(t("0s"), t("15s"))
    beats: eks.TimeSet = t.beats("downbeat")[0:8]
    target: eks.Target = by.text("Manuel").near(by.text("Şablon ile"))
    return start, window, beats, target
