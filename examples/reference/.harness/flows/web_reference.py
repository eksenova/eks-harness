import eks
from eks import by, expect, t
from eks.checks import use


@eks.flow(app="reference-web", viewport="desktop", timeout=t("10s"))
def web_reference(s: eks.Session):
    s.ui.open_url("/")
    s.ui.wait_for(by.test_id("invoice-row").first)
    expect(by.test_id("invoice-row")).count(3)
    expect(by.role("heading", name="Faturalar")).visible()
    expect.eventually(by.test_id("rate")).text(contains="2.450,10")

    s.ui.tap(by.test_id("save"))
    expect(by.role("alert")).text("Müşteri ve tutar zorunlu")
    s.ui.fill(by.label("Müşteri"), "Moda Şube")
    s.ui.type(by.css("#amount"), "125.50")
    s.ui.select(by.css("#currency"), "USD")
    s.ui.check(by.css("#e-archive"))
    draft = s.ui.state("draft")
    expect(s.ui.state("draft.customer")).eq("Moda Şube")
    expect(s.ui.state("draft.currency")).eq("USD")
    expect(s.ui.state("draft.archive")).eq(True)
    expect(by.label("Tutar")).value("125.50")
    s.ui.tap(by.test_id("save"))
    expect.eventually(by.test_id("toast")).text(contains="kaydedildi")
    expect(by.test_id("invoice-row")).count(4)

    s.ui.fill(by.placeholder("Ara"), "kadıköy")
    expect(by.test_id("invoice-row")).count(1)
    s.ui.clear(by.placeholder("Ara"))
    expect(by.test_id("invoice-row")).count(4)

    s.ui.tap(by.role("button", name="Sil").within(by.test_id("invoice-row").nth(1)))
    expect(by.role("dialog")).visible()
    expect(by.role("dialog")).text(contains="Beyaz Altın Toptan")
    s.ui.tap(by.role("button", name="Vazgeç"))
    expect(by.role("dialog")).hidden()

    s.net.route("GET", "/api/rates").respond(status=200, json={"gold": 3000})
    s.ui.reload()
    expect.eventually(by.test_id("rate")).text(contains="3.000,00")
    s.net.unroute()

    s.checkpoint("liste", checks=["console_errors", "page_errors", "failed_requests", "layout_spill", "horizontal_scroll",
                                   "broken_images", use("route", expected="/")])
    s.shot("liste")
    s.dump("a11y")
    s.har()

    with s.record("web-tur", pointer=True):
        s.overlay.title("Referans faturalar", hold=t("1s"))
        s.overlay.callout(by.test_id("save"), "Yeni faturayı kaydeder", hold=t("1s"))
        s.ui.tap(by.role("button", name="Sil").first)
        s.hold(t("500ms"))
        s.ui.tap(by.role("button", name="Evet, sil"))
        expect.eventually(by.test_id("toast")).text(contains="silindi")

    s.overlay.highlight(by.test_id("invoice-list"), keep=True)
    s.overlay.icon(by.test_id("save"), "check", keep=True)
    s.overlay.arrow(by.placeholder("Ara"), by.test_id("save"), text="Önce ara", keep=True)
    s.shot("overlay")
    s.overlay.clear()

    route = s.ui.route()
    return {"route": route, "draft": draft, "url": s.ui.url()}
