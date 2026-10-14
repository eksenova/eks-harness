import eks
from eks import expect, t
from eks.checks import use


@eks.flow(app="web-app", seed="standard", as_="harman-admin", viewport="cookbook-tablet")
def extending(s: eks.Session):
    s.net.shape("cookbook-edge")
    rate = s.cookbook.rate(currency="XAU", base="TRY")
    s.cookbook.new_invoice("kadikoy", amount="5")
    s.ui.tap("chip=Bekleyen")
    s.overlay.show(eks.overlay.custom("badge", anchor="chip=Bekleyen", text="Yeni", hold=t("2s")))
    s.checkpoint("fatura", checks=[use("contrast", minimum=7.0)])
    expect(rate.rate).matches(r"^\d+(\.\d+)?$")
    return {"rate": rate}
