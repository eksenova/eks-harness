from typing import Annotated

import eks

FederationRef = Annotated[eks.ResourceRef, eks.refers_to("federation")]
TenantRef = Annotated[eks.ResourceRef, eks.refers_to("tenant")]


@eks.resource("host_persona", persona=True, description="A host (Eksenova staff) user")
class HostPersona(eks.Resource):
    pass


@eks.resource("federation", description="A reseller or partner federation")
class Federation(eks.Resource):
    type: str
    permissions: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@eks.resource("tenant", description="A jeweller tenant")
class Tenant(eks.Resource):
    name: str
    reseller: FederationRef | None = None


@eks.resource("persona", parent="tenant", persona=True, description="A tenant user")
class TenantPersona(eks.Resource):
    role: str


@eks.resource("accountant", description="An accounting office")
class Accountant(eks.Resource):
    name: str


@eks.resource("serves", parent="accountant", keyed=False, positional=("tenant",),
              description="An accountant serving a tenant")
class Serves(eks.Resource):
    tenant: TenantRef


@eks.resource("bank_account", parent="tenant", description="A bank account of a tenant")
class BankAccount(eks.Resource):
    currency: str
    opening: int = 0


@eks.resource("transactions", parent="bank_account", keyed=False, description="Generated bank transactions")
class Transactions(eks.Resource):
    n: int
    profile: str


@eks.resource("invoices", parent="tenant", keyed=False, description="Invoices in every requested state")
class Invoices(eks.Resource):
    states: str | list[str]
    per_state: int
