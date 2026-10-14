import eks
from eks import by


@eks.flow(app="mobile-app", seed="standard")
def targets_mobile(s: eks.Session):
    s.ui.tap(by.kind("button").within(by.test_id("invoice-form")).first)
    s.ui.tap(by.test_id("credit-add-submit"))
    s.ui.tap(by.role("button", name="Sunuyu iptal et"))
    s.ui.tap(by.text("Manuel").near(by.text("Şablon ile")))
    s.ui.swipe("up", target=by.test_id("invoice-list"))
    s.ui.long_press(by.point(120, 480))
