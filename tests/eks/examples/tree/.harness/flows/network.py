import eks
from eks import t


@eks.flow(app="web-app", seed="standard")
def network(s: eks.Session):
    s.net.route("GET", "/api/bank-transactions*").delay(t("800ms"))
    s.net.route("POST", "/api/invoices").respond(status=500, json={"error": "x"})
    s.net.route("GET", "/api/rates").patch(lambda_path="items[0].rate", value="2450.10")
    s.net.shape("3g")                                  # deterministic profile
    with s.net.cassette("banka-arama", mode="replay"): # record | replay | auto
        s.ui.nav("/tenant/bank/transactions")
    s.inject.script("window.__flags = {beta: true}", when="init")
