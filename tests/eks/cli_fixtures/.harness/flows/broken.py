import eks


@eks.flow(app="web-app", seed="standard", as_="nobody")
def broken(s: eks.Session):
    s.ui.nav("/tenant/settings/branches")
