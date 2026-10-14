import eks
from eks import by, expect, t


@eks.flow(app="mobile-app", seed="standard", as_="harman-admin")
def surface_mobile(s: eks.Session):
    s.ui.deeplink("eksengold://invoice/new")
    s.fake.arm("camera", media=eks.media.source("fixtures/kimlik-on.jpg"), mode="success")
    s.fake.arm("nfc", fixture="kimlik-cip.json")
    s.ui.tap(by.test_id("scan-id"))
    s.ui.swipe("left", target=by.test_id("id-pages"))
    s.fake.clear()

    s.mobile.dispatch({"type": "invoice/reset"})
    draft = s.mobile.state("invoice.draft")
    s.mobile.patch(by.test_id("clock"), text="09:41", key="clock")
    s.mobile.mount("DebugBanner", props={"text": "staging"}, key="banner")
    s.mobile.idle(timeout=t("5s"))
    s.ui.fill("#amount", "5")
    s.ui.tap(by.kind("button").within(by.test_id("invoice-form")).last)
    s.mobile.unpatch("clock")

    expect(by.test_id("invoice-total")).text("5,00 ₺")
    s.as_("harman-admin")
    s.checkpoint("fatura-mobil")
    return {"draft": draft}
