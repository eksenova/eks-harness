from collections.abc import Mapping
from typing import Any

import eks
from eks_harness.ir.ops.data import ResourceRef
from eks_harness.plugins.sdk import BackendInfo, BackendSpec


@eks.backend("eksenapi", description="The API the design examples run against")
class ExampleApi:
    url = "http://127.0.0.1:7122"

    def describe(self) -> dict[str, Any]:
        return {"name": "eksenapi", "url": self.url}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return self.url

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec(info=BackendInfo(url=self.url))

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


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
    reseller: ResourceRef | None = None


@eks.resource("persona", parent="tenant", persona=True, description="A tenant user")
class TenantPersona(eks.Resource):
    role: str


@eks.resource("accountant", description="An accounting office")
class Accountant(eks.Resource):
    name: str


@eks.resource("serves", parent="accountant", keyed=False, positional=("tenant",),
              description="An accountant serving a tenant")
class Serves(eks.Resource):
    tenant: ResourceRef


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
