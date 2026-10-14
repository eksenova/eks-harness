import eks


@eks.resource("cli_tenant", description="A tenant of the CLI fixture seed")
class CliTenant(eks.Resource):
    name: str = ""


@eks.resource("cli_persona", parent="cli_tenant", persona=True, description="A user of a CLI fixture tenant")
class CliPersona(eks.Resource):
    role: str = "admin"


@eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
def standard(d: eks.Data):
    harman = d.cli_tenant("harman", name="Harman Kuyumculuk")
    harman.cli_persona("harman-admin", role="admin")
    harman.cli_persona("harman-kasiyer", role="cashier")
