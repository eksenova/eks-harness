import eks
from eks import by, expect, t


@eks.flow(app="web-app", seed="standard", as_="harman-admin", viewport="desktop", timeout=t("10s"))
def surface_web(s: eks.Session):
    info = s.backend.info
    branches = s.api.get("/api/tenant-branches", query={"MaxResultCount": 50})
    created = s.api.post("/api/tenant-branches", json={"name": "Moda"})
    s.api.put(f"/api/tenant-branches/{created.id}", json={"name": "Moda Şubesi"})

    s.net.route("GET", "/api/rates").respond(status=200, json={"items": [{"rate": "2450.10"}]})
    s.net.shape("3g")
    s.inject.script("window.__flags = {beta: true}", when="init")
    s.inject.style("[data-testid='clock'] { visibility: hidden; }")

    s.ui.nav("/tenant/invoice/list", params={"branchId": branches.items.first.id})
    s.ui.back()
    s.ui.reload()
    s.ui.open_url("http://localhost:3000/tenant/settings/branches")
    s.ui.settle(strict=True)

    s.ui.tap("#new-invoice")
    s.ui.double_tap(by.test_id("invoice-row").first)
    s.ui.long_press(by.test_id("invoice-row").last, hold=t("800ms"))
    s.ui.hover(by.role("button", name="Filtrele"))
    s.ui.fill(by.label("E-posta"), "muhasebe@harman.com.tr")
    s.ui.type(by.placeholder("Ara"), "Kadıköy", pace="demo")
    s.ui.clear(by.placeholder("Ara"))
    s.ui.key("Enter")
    s.ui.select("#currency", "TRY")
    s.ui.check("#e-archive")
    s.ui.choose(by.test_id("branch-picker"), "Kadıköy")
    s.ui.scroll(by.test_id("invoice-list"), to="bottom")
    s.ui.scroll(by.test_id("invoice-list"), by=(0, 400))
    s.ui.drag(by.test_id("column-amount"), to=by.test_id("column-date"))

    title = s.ui.text(by.role("heading"))
    amount = s.ui.value("#amount")
    href = s.ui.attr(by.role("link", name="Faturalar"), "href")
    rows = s.ui.count(by.test_id("invoice-row"))
    empty = s.ui.exists("#empty-state")
    dialog = s.ui.visible(by.role("dialog"))
    outline = s.ui.tree(max_lines=200)
    url = s.ui.url()
    route = s.ui.route()
    cart = s.ui.state("invoice.draft")
    box = s.ui.layout("#total")
    flags = s.eval("() => window.__flags")

    s.ui.wait_for(by.test_id("invoice-row"), state="visible", timeout=t("5s"))
    s.ui.wait_gone("#spinner")
    s.ui.wait_url("/tenant/invoice/*")

    expect(by.text("Kadıköy")).visible()
    expect(by.role("dialog")).hidden()
    expect(by.role("heading")).text("Faturalar")
    expect(by.test_id("invoice-row")).count(at_least=1)
    expect("#amount").value("2450.10")
    expect(title).eq("Faturalar")
    expect(url).contains("/tenant/invoice")
    expect(amount).matches(r"^\d+\.\d{2}$")
    expect.soft(rows).gt(0)
    expect.eventually(by.test_id("toast")).hidden()

    s.checkpoint("liste")
    s.checkpoint("liste-telefon", viewport="phone")
    shot = s.shot("toplam", element="#total")
    s.dump("dom")
    s.dump("a11y")
    s.dump("mhtml")
    s.har()

    with s.record("fatura", pace="demo", viewport="phone", pointer=True):
        s.overlay.title("Yeni fatura", hold=t("2s"))
        s.ui.tap("#new-invoice")
        s.hold(t("1s"))
        s.overlay.callout("#total", "Anlık kur", hold=t("2s"))

    s.web.viewport("tablet")
    s.web.logs("console")
    s.debug.break_("before-delete")
    s.api.delete(f"/api/tenant-branches/{created.id}")

    return {
        "info": info, "branches": branches, "href": href, "empty": empty, "dialog": dialog,
        "outline": outline, "route": route, "cart": cart, "box": box, "flags": flags, "shot": shot,
    }
