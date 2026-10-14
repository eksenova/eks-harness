import eks
from eks import by


@eks.flow(app="web-app", seed="standard")
def targets(s: eks.Session):
    s.ui.tap(by.test_id("credit-add-submit"))
    s.ui.tap("#credit-add-submit")
    s.ui.tap(by.role("button", name="Sunuyu iptal et"))
    s.ui.tap("role=button:Sunuyu iptal et")
    s.ui.tap(by.text("Kadıköy"))
    s.ui.tap(by.text("Kadı", exact=False))
    s.ui.tap("Kadıköy")
    s.ui.fill(by.label("E-posta"), "muhasebe@harman.com.tr")
    s.ui.fill(by.placeholder("Ara"), "Kadıköy")
    s.ui.tap(by.css("li[data-package-id='3']"))
    s.ui.tap(by.test_id("list").within(by.role("dialog")).nth(2))
    s.ui.tap(by.text("Manuel").near(by.text("Şablon ile")))
    s.ui.tap(by.point(120, 480))
