import eks
from eks import by


@eks.flow(app="mobile-app")
def tapper(s: eks.Session):
    s.ui.tap(by.test_id("save"))
