import eks
from eks import by, expect, t
from eks.checks import use


@eks.flow(app="reference-ios", timeout=t("15s"))
def ios_reference(s: eks.Session):
    expect(by.test_id("title")).text("Faturalar")
    expect(by.test_id("invoice-row")).count(2)

    s.ui.tap(by.test_id("save"))
    expect(by.test_id("status")).text("Müşteri zorunlu")
    s.ui.fill(by.test_id("customer"), "Moda Şube")
    s.ui.tap(by.test_id("save"))
    expect.eventually(by.test_id("status")).text("Kaydedildi: Moda Şube")
    expect(by.test_id("invoice-row")).count(3)
    s.ui.tap(by.text("Sil").last)
    expect.eventually(by.test_id("invoice-row")).count(2)

    s.ui.check(by.test_id("e-archive"))
    archive = s.ui.state("archive")
    expect(archive).eq(True)
    s.mobile.dispatch({"type": "invoice/reset"})
    expect.eventually(by.test_id("invoice-row")).count(2)

    s.net.route("GET", "https://reference.invalid/api/rates").respond(status=200, json={"gold": 3100})
    s.ui.tap(by.test_id("rate-refresh"))
    expect.eventually(by.test_id("rate")).text("Kur: 3100")
    s.net.unroute()

    s.ui.tap(by.test_id("alert"))
    expect.eventually(by.text("Silme onayı").native, timeout=t("10s")).visible()
    s.shot("uyari")
    s.ui.tap(by.text("Evet, sil").native)
    expect.eventually(by.test_id("status")).text("Silindi")

    s.ui.tap(by.test_id("location"))
    expect.eventually(by.text("Allow While Using App").native, timeout=t("10s")).visible()
    s.shot("konum-izni")
    s.ui.tap(by.text("Allow While Using App").native)
    expect.eventually(by.test_id("permission")).text("Konum izni: verildi")

    s.ui.nav("Detail", params={"id": 5})
    expect.eventually(by.test_id("detail-title")).text("Fatura #5")
    s.ui.back()
    s.ui.deeplink("eksref://invoice/9")
    expect.eventually(by.test_id("detail-title")).text("Fatura #9")
    s.ui.back()

    with s.record("ios-tur", pointer=True):
        s.overlay.caption("Referans uygulaması", keep=True)
        s.overlay.callout(by.test_id("save"), "Kaydet", hold=t("1s"))
        s.ui.tap(by.test_id("detail"))
        s.hold(t("1s"))
    s.checkpoint("detay", checks=["page_errors", use("route", expected="Detail")])
    s.shot("detay")
    return {"archive": archive, "route": s.ui.route()}
