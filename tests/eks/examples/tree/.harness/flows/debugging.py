import eks


@eks.flow(app="web-app", seed="standard")
def debugging(s: eks.Session):
    s.debug.break_("before-submit")
