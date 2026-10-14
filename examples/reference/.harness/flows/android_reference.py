import eks
from eks import by, expect, t
from eks.checks import use


@eks.flow(app="reference-android", timeout=t("15s"))
def android_reference(s: eks.Session):
    s.ui.settle()
    expect(by.test_id("title")).text("Faturalar")
    expect(by.text("Harman Kuyumculuk")).visible()

    s.ui.tap(by.test_id("save"))
    expect(by.test_id("status")).text("Müşteri zorunlu")
    s.ui.fill(by.test_id("customer"), "Moda Sube")
    s.ui.tap(by.test_id("save"))
    expect.eventually(by.test_id("status")).text("Kaydedildi: Moda Sube")
    expect(by.text("Moda Sube")).visible()

    s.ui.tap(by.test_id("delete"))
    expect.eventually(by.text("Silme onayı")).visible()
    s.shot("uygulama-penceresi")
    s.ui.tap(by.text("evet, sil", exact=False))
    expect.eventually(by.test_id("status")).text("Silindi")

    s.ui.tap(by.test_id("camera"))
    expect.eventually(by.text("While using the app").native, timeout=t("10s")).visible()
    s.shot("izin-penceresi")
    s.ui.tap(by.text("While using the app").native)
    expect.eventually(by.test_id("permission")).text("Kamera izni: verildi")

    s.ui.deeplink("eksref://invoice/7")
    expect.eventually(by.test_id("route")).text("invoice/7")

    s.checkpoint("son", checks=["page_errors", use("route", expected="*MainActivity")])
    with s.record("android-tur"):
        s.ui.tap(by.test_id("save"))
        s.hold(t("1s"))
        s.ui.swipe("up")
        s.hold(t("500ms"))
    return {"route": s.ui.route()}
