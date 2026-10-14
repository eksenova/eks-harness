import eks


@eks.flow(app="web-app")
def raises(s: eks.Session):
    with eks.parallel():
        s.ui.nav("/a")
        with eks.parallel():
            s.ui.nav("/b")
