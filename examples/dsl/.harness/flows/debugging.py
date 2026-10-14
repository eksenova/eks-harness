import eks


@eks.flow(app="web-app", seed="standard")
def debugging(s: eks.Session):
    s.debug.pause_on_error()
    s.ui.nav("/tenant/invoice/new")
    s.ui.fill("#amount", "2450.10")
    s.debug.break_("before-submit")
    s.ui.tap("#submit")
