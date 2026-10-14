import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks.data import Data, ResourceHandle
from eks.discovery import load_file
from eks.ref import node_of
from eks.tracing import TraceError
from eks_harness.ir.hashing import derive_id
from eks_harness.ir.ops.data import (
    RESERVED_CHILD_KINDS,
    RESERVED_ROOT_KINDS,
    ResourceRef,
)
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.refs import RJson
from eks_harness.ir.registry import Registry, RegistryError, contributing


def register_kinds() -> None:
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


def creates(plan: Plan) -> dict[str, Node]:
    return {str(node.args["path"]): node for node in plan.nodes if node.op == "data.create"}


def ref_to(node: Node) -> dict[str, Any]:
    return {"$ref": node.id, "path": []}


def plan_standard() -> Plan:
    @eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
    def standard(d: eks.Data):
        host = d.host_persona("host")  # noqa: F841
        bayi = d.federation("anadolu-bayi", type="Reseller", permissions=["*"],
                            exclude=["Kts.Federations.*"])
        harman = d.tenant("harman", name="Harman Kuyumculuk", reseller=bayi)
        harman.persona("harman-admin", role="admin")
        d.accountant("yildiz", name="Yıldız Mali Müşavirlik").serves(harman)
        kt = harman.bank_account("kuveyt", currency="TRY", opening=250_000)
        kt.transactions(n=120, profile="kuyumcu")
        harman.invoices(states="*", per_state=3)

    return eks.plan(standard)


def test_design_7_seed(eks_registry: Registry) -> None:
    register_kinds()
    plan = plan_standard()
    assert plan.kind == "seed"
    assert plan.name == "standard"
    assert plan.personas == ["host", "harman-admin"]
    assert plan.clock.now == "2026-01-15T09:00:00+03:00"
    assert plan.clock.seed == 2026
    (backend,) = plan.resources
    assert backend.kind == "backend"
    assert backend.definition == "eksenapi"
    assert backend.seed == "standard"
    assert backend.app is None
    assert backend.session is None

    nodes = creates(plan)
    assert list(nodes) == [
        "host_persona:host",
        "federation:anadolu-bayi",
        "tenant:harman",
        "tenant:harman/persona:harman-admin",
        "accountant:yildiz",
        "accountant:yildiz/serves#0",
        "tenant:harman/bank_account:kuveyt",
        "tenant:harman/bank_account:kuveyt/transactions#0",
        "tenant:harman/invoices#0",
    ]
    assert len(plan.nodes) == len(nodes)
    for path, node in nodes.items():
        assert node.id == derive_id("n", f"seed:standard/{path}#0")
        assert node.uses == [backend.id]
        assert node.out == f"resource:{node.args['kind']}"

    bayi = nodes["federation:anadolu-bayi"]
    harman = nodes["tenant:harman"]
    yildiz = nodes["accountant:yildiz"]
    kuveyt = nodes["tenant:harman/bank_account:kuveyt"]
    assert nodes["host_persona:host"].args == {
        "kind": "host_persona", "key": "host", "path": "host_persona:host", "fields": {}, "phase": "api"}
    assert bayi.args == {
        "kind": "federation", "key": "anadolu-bayi", "path": "federation:anadolu-bayi",
        "fields": {"type": "Reseller", "permissions": ["*"], "exclude": ["Kts.Federations.*"]}, "phase": "api"}
    assert harman.args == {
        "kind": "tenant", "key": "harman", "path": "tenant:harman",
        "fields": {"name": "Harman Kuyumculuk", "reseller": ref_to(bayi)}, "phase": "api"}
    assert nodes["tenant:harman/persona:harman-admin"].args == {
        "kind": "persona", "key": "harman-admin", "path": "tenant:harman/persona:harman-admin",
        "parent": ref_to(harman), "fields": {"role": "admin"}, "phase": "api"}
    assert nodes["accountant:yildiz/serves#0"].args == {
        "kind": "serves", "path": "accountant:yildiz/serves#0", "parent": ref_to(yildiz),
        "fields": {"tenant": ref_to(harman)}, "phase": "api"}
    assert kuveyt.args["fields"] == {"currency": "TRY", "opening": 250_000}
    assert nodes["tenant:harman/bank_account:kuveyt/transactions#0"].args["fields"] == {"n": 120, "profile": "kuyumcu"}
    assert nodes["tenant:harman/invoices#0"].args["fields"] == {"states": "*", "per_state": 3}

    # graph ordering: only parents and referenced resources, never a program chain
    assert nodes["host_persona:host"].after == []
    assert bayi.after == []
    assert harman.after == [bayi.id]
    assert yildiz.after == []
    assert nodes["tenant:harman/persona:harman-admin"].after == [harman.id]
    assert nodes["accountant:yildiz/serves#0"].after == [yildiz.id, harman.id]
    assert kuveyt.after == [harman.id]
    assert nodes["tenant:harman/bank_account:kuveyt/transactions#0"].after == [kuveyt.id]
    assert nodes["tenant:harman/invoices#0"].after == [harman.id]


def test_seed_plans_are_byte_identical(eks_registry: Registry) -> None:
    register_kinds()
    first = plan_standard()
    second = plan_standard()
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash


SWAP_A = """
import eks

@eks.seed("swap", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
def swap(d: eks.Data):
    bayi = d.federation("anadolu-bayi", type="Reseller")
    d.accountant("yildiz", name="Yildiz")
    harman = d.tenant("harman", name="Harman", reseller=bayi)
    harman.invoices(states="*", per_state=1)
    harman.invoices(states=["draft"], per_state=2)
"""

SWAP_B = """
import eks


@eks.seed("swap", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
def swap(d: eks.Data):

    # the accountant is independent of the federation, so declaring it first changes no id
    d.accountant("yildiz", name="Yildiz")

    bayi = d.federation("anadolu-bayi", type="Reseller")


    harman = d.tenant("harman", name="Harman", reseller=bayi)
    harman.invoices(states="*", per_state=1)
    harman.invoices(states=["draft"], per_state=2)
"""


def plan_file(path: Path, source: str) -> Plan:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    (entry,) = load_file(path)
    return eks.plan(entry)


def test_ids_come_from_paths_not_lines(eks_registry: Registry, tmp_path: Path) -> None:
    register_kinds()
    seed_file = tmp_path / "swap.py"
    before = plan_file(seed_file, SWAP_A)
    after = plan_file(seed_file, SWAP_B)
    ids_before = {path: node.id for path, node in creates(before).items()}
    ids_after = {path: node.id for path, node in creates(after).items()}
    assert ids_before == ids_after
    assert set(ids_before) == {
        "federation:anadolu-bayi", "accountant:yildiz", "tenant:harman", "tenant:harman/invoices#0",
        "tenant:harman/invoices#1"}
    hashes_before = {path: node.hash for path, node in creates(before).items()}
    hashes_after = {path: node.hash for path, node in creates(after).items()}
    assert hashes_before == hashes_after
    assert [node.args["path"] for node in after.nodes][:2] == ["accountant:yildiz", "federation:anadolu-bayi"]


def test_clock_and_extend(eks_registry: Registry) -> None:
    register_kinds()
    seen: dict[str, Any] = {}

    @eks.seed("feature", backend="eksenapi", now="2026-03-01T10:30+03:00", seed=7)
    def feature(d: eks.Data):
        seen["now"] = d.now
        seen["seed"] = d.seed
        seen["first"] = d.extend("base")
        seen["again"] = d.extend(base="base")
        d.tenant("yeni", name="Yeni Kuyumcu")

    plan = eks.plan(feature)
    assert seen["now"] == "2026-03-01T10:30:00+03:00"
    assert seen["seed"] == 7
    assert seen["first"] is seen["again"]
    extend, tenant = plan.nodes
    assert extend.op == "data.extend"
    assert extend.args == {"base": "base"}
    assert extend.id == derive_id("n", "seed:feature/data.extend:base#0")
    assert extend.uses == tenant.uses
    assert extend.out == "none"
    # the engine restores every data.extend before any data.create, and the seed declares its extends first
    assert [node.op for node in plan.nodes] == ["data.extend", "data.create"]
    assert tenant.after == []
    assert tenant.args["fields"] == {"name": "Yeni Kuyumcu", "reseller": None}


def test_extend_after_resources_is_refused(eks_registry: Registry) -> None:
    register_kinds()

    @eks.seed("late", backend="eksenapi")
    def late(d: eks.Data):
        d.tenant("yeni", name="Yeni Kuyumcu")
        d.extend("standard")

    with pytest.raises(TraceError, match=r"d.extend\('standard'\) comes after resources of seed 'late'"):
        eks.plan(late)


def test_repeated_extend_after_resources_returns_the_first(eks_registry: Registry) -> None:
    register_kinds()
    seen: dict[str, Any] = {}

    @eks.seed("again", backend="eksenapi", base="standard")
    def again(d: eks.Data):
        d.tenant("yeni", name="Yeni Kuyumcu")
        seen["ref"] = d.extend("standard")

    plan = eks.plan(again)
    extend, _tenant = plan.nodes
    assert node_of(seen["ref"]) == extend.id


def extend_in_when_body(d: eks.Data) -> None:
    bayi = d.federation("anadolu-bayi", type="Reseller")
    with eks.when(bayi.ref.active):
        d.extend("standard")
    d.extend("standard")


def extend_in_literal_each_body(d: eks.Data) -> None:
    with eks.each([1, 2]):
        d.extend("standard")


def extend_in_runtime_each_body(d: eks.Data) -> None:
    bayi = d.federation("anadolu-bayi", type="Reseller")
    with eks.each(bayi.ref.members):
        d.extend("standard")


@pytest.mark.parametrize("body", [extend_in_when_body, extend_in_literal_each_body, extend_in_runtime_each_body])
def test_extend_outside_the_seed_root_is_refused(eks_registry: Registry, body: Any) -> None:
    # a base snapshot is restored once, never per iteration or under a runtime condition
    register_kinds()

    @eks.seed("scoped", backend="eksenapi")
    def scoped(d: eks.Data):
        body(d)

    with pytest.raises(TraceError, match=r"d.extend\('standard'\) is called inside .* top level of the seed"):
        eks.plan(scoped)


def test_explicit_none_fields_stay_in_the_plan(eks_registry: Registry) -> None:
    @eks.resource("branch", description="A branch with an optional opening balance")
    class Branch(eks.Resource):
        name: str
        opening: int | None = 5

    @eks.seed("branches", backend="eksenapi")
    def branches(d: eks.Data):
        d.branch("default", name="Varsayilan")
        d.branch("empty", name="Bos", opening=None)

    nodes = creates(eks.plan(branches))
    assert nodes["branch:default"].args["fields"] == {"name": "Varsayilan", "opening": 5}
    assert nodes["branch:empty"].args["fields"] == {"name": "Bos", "opening": None}
    del Branch


def test_reserved_kind_names_are_the_builder_members() -> None:
    # a kind named like one of these would be shadowed by the member, so the registry refuses them
    def members(cls: type) -> set[str]:
        return {name for name in vars(cls) if not name.startswith("_")}

    assert members(Data) == RESERVED_ROOT_KINDS
    assert members(ResourceHandle) == RESERVED_CHILD_KINDS


def test_base_option_records_extend_first(eks_registry: Registry) -> None:
    register_kinds()

    @eks.seed("branch", backend="eksenapi", base="standard")
    def branch(d: eks.Data):
        d.extend("standard")
        d.tenant("dal", name="Dal")

    plan = eks.plan(branch)
    extend, tenant = plan.nodes
    assert extend.op == "data.extend"
    assert extend.args == {"base": "standard"}
    assert tenant.args["path"] == "tenant:dal"
    assert plan.clock.now is None
    assert plan.clock.seed == 0


def test_extend_errors(eks_registry: Registry) -> None:
    @eks.seed("selfish", backend="eksenapi")
    def selfish(d: eks.Data):
        d.extend("selfish")

    @eks.seed("badname", backend="eksenapi")
    def badname(d: eks.Data):
        d.extend("not a seed")

    with pytest.raises(TraceError, match="cannot extend itself"):
        eks.plan(selfish)
    with pytest.raises(ValueError, match="another seed"):
        eks.plan(badname)


def test_generic_create_and_keyless_counters(eks_registry: Registry) -> None:
    register_kinds()
    handles: list[ResourceHandle] = []

    @eks.seed("generic", backend="eksenapi")
    def generic(d: eks.Data):
        beyaz = d.create("tenant", "beyaz", name="Beyaz Altın")
        admin = d.create("persona", "beyaz-admin", parent=beyaz, role="admin")
        first = d.create("invoices", parent=beyaz, states="*", per_state=1)
        second = beyaz.invoices(states=["paid"], per_state=2)
        harman = d.tenant("harman", name="Harman")
        other = harman.invoices(states="*", per_state=1)
        acc = d.accountant("yildiz", name="Yildiz")
        acc.serves(beyaz)
        acc.serves(tenant=harman)
        handles.extend([beyaz, admin, first, second, other])

    plan = eks.plan(generic)
    beyaz, admin, first, second, other = handles
    assert (beyaz.kind, beyaz.key, beyaz.path) == ("tenant", "beyaz", "tenant:beyaz")
    assert (admin.kind, admin.key, admin.path) == ("persona", "beyaz-admin", "tenant:beyaz/persona:beyaz-admin")
    assert (first.key, first.path) == (None, "tenant:beyaz/invoices#0")
    assert second.path == "tenant:beyaz/invoices#1"
    assert other.path == "tenant:harman/invoices#0"
    assert node_of(admin.ref) == creates(plan)[admin.path].id
    assert admin.__eks_encode__() == {"$ref": node_of(admin.ref), "path": []}
    assert repr(admin) == "ResourceHandle(tenant:beyaz/persona:beyaz-admin)"
    assert plan.personas == ["beyaz-admin"]
    nodes = creates(plan)
    assert nodes["accountant:yildiz/serves#0"].args["fields"] == {"tenant": ref_to(nodes["tenant:beyaz"])}
    assert nodes["accountant:yildiz/serves#1"].args["fields"] == {"tenant": ref_to(nodes["tenant:harman"])}


def test_handles_and_refs_flow_into_fields(eks_registry: Registry) -> None:
    register_kinds()

    @eks.resource("ledger", description="A ledger entry that refers to other resources")
    class Ledger(eks.Resource):
        owner: ResourceRef
        amount: RJson = None

    @eks.seed("refs", backend="eksenapi")
    def refs(d: eks.Data):
        harman = d.tenant("harman", name="Harman")
        kt = harman.bank_account("kuveyt", currency="TRY")
        d.ledger("opening", owner=harman, amount=kt.ref.balance)

    nodes = creates(eks.plan(refs))
    harman = nodes["tenant:harman"]
    kuveyt = nodes["tenant:harman/bank_account:kuveyt"]
    ledger = nodes["ledger:opening"]
    assert ledger.args["fields"] == {"owner": ref_to(harman), "amount": {"$ref": kuveyt.id, "path": ["balance"]}}
    assert ledger.after == [harman.id, kuveyt.id]
    del Ledger


def test_seed_outputs_encode_handles(eks_registry: Registry) -> None:
    register_kinds()

    @eks.seed("outputs", backend="eksenapi")
    def outputs(d: eks.Data):
        harman = d.tenant("harman", name="Harman")
        return {"tenant": harman, "id": harman.ref.id}

    plan = eks.plan(outputs)
    (tenant,) = plan.nodes
    assert plan.outputs == {"tenant": ref_to(tenant), "id": {"$ref": tenant.id, "path": ["id"]}}


@pytest.mark.parametrize(
    ("body", "error", "match"),
    [
        (lambda d: d.bogus("x"), AttributeError, "unknown resource kind 'bogus' at the seed root"),
        (lambda d: d.tenent("x", name="x"), AttributeError, "did you mean 'tenant'"),
        (lambda d: d.persona("x", role="admin"), AttributeError, "created under a tenant handle"),
        (lambda d: d.tenant("t", name="T").federation("f", type="x"), AttributeError,
         "kinds that fit here: bank_account, invoices, persona"),
        (lambda d: d.tenant(name="T"), TypeError, "needs the resource key as its first argument"),
        (lambda d: d.tenant(3, name="T"), TypeError, "literal string"),
        (lambda d: d.tenant("a/b", name="T"), ValidationError, "key"),
        (lambda d: d.tenant("t", "T"), TypeError, "positional field"),
        (lambda d: d.accountant("a", name="A").serves(d.tenant("t", name="T"), tenant=None), TypeError,
         "both positionally and by keyword"),
        (lambda d: d.tenant("t", name="T", city="Kayseri"), ValidationError, "city"),
        (lambda d: d.tenant("t"), ValidationError, "name"),
        (lambda d: [d.tenant("t", name="T"), d.tenant("t", name="U")], TraceError, "declared twice"),
        (lambda d: [d.tenant("a", name="A").persona("admin", role="admin"),
                    d.tenant("b", name="B").persona("admin", role="viewer")], TraceError,
         ("persona 'admin' is declared twice in seed 'broken': 'tenant:a/persona:admin' and "
          "'tenant:b/persona:admin'")),
        (lambda d: [d.host_persona("host"), d.tenant("a", name="A").persona("host", role="admin")], TraceError,
         "persona 'host' is declared twice in seed 'broken': 'host_persona:host' and 'tenant:a/persona:host'"),
        (lambda d: d.create("tenantz", "t"), RegistryError, "unknown resource kind"),
        (lambda d: d.create("persona", "p", role="admin"), TraceError, "under a tenant handle"),
        (lambda d: d.create("tenant", name="T"), TypeError, "needs a key"),
        (lambda d: d.create("invoices", "i", parent=d.tenant("t", name="T"), states="*", per_state=1),
         TypeError, "keyless"),
        (lambda d: d.create("tenant", "t", parent="x", name="T"), TypeError, "resource handle"),
    ],
)
def test_seed_errors(eks_registry: Registry, body: Any, error: type[Exception], match: str) -> None:
    register_kinds()

    @eks.seed("broken", backend="eksenapi")
    def broken(d: eks.Data):
        body(d)

    with pytest.raises(error, match=match):
        eks.plan(broken)


def literal_each_body(d: eks.Data) -> None:
    harman = d.tenant("harman", name="Harman")
    with eks.each([1, 2]) as n:
        harman.bank_account("kuveyt", currency="TRY", opening=n)


def keyless_each_body(d: eks.Data) -> None:
    harman = d.tenant("harman", name="Harman")
    with eks.each(["Draft", "Sent"]) as state:
        harman.invoices(states=state, per_state=1)


def runtime_each_body(d: eks.Data) -> None:
    bayi = d.federation("anadolu-bayi", type="Reseller")
    with eks.each(bayi.ref.members) as member:
        d.tenant("member", name=member.name)


def when_body(d: eks.Data) -> None:
    bayi = d.federation("anadolu-bayi", type="Reseller")
    with eks.when(bayi.ref.active):
        d.tenant("harman", name="Harman")


def parallel_body(d: eks.Data) -> None:
    with eks.parallel():
        d.tenant("harman", name="Harman")


@pytest.mark.parametrize(
    ("body", "path"),
    [
        (literal_each_body, "tenant:harman/bank_account:kuveyt"),
        (keyless_each_body, "tenant:harman/invoices#0"),
        (runtime_each_body, "tenant:member"),
        (when_body, "tenant:harman"),
        (parallel_body, "tenant:harman"),
    ],
)
def test_resources_outside_the_seed_root_are_refused(eks_registry: Registry, body: Any, path: str) -> None:
    # each copies would share one path (and its generated ids), so only the seed's top level may declare resources
    register_kinds()

    @eks.seed("scoped", backend="eksenapi")
    def scoped(d: eks.Data):
        body(d)

    with pytest.raises(TraceError, match=rf"resource '{path}' is declared inside .* plain Python for loop"):
        eks.plan(scoped)


def test_python_loops_give_each_resource_its_own_path(eks_registry: Registry) -> None:
    register_kinds()

    @eks.seed("looped", backend="eksenapi")
    def looped(d: eks.Data):
        harman = d.tenant("harman", name="Harman")
        for code in ("kuveyt", "ziraat"):
            harman.bank_account(code, currency="TRY", opening=1000)
        for state in ("Draft", "Sent"):
            harman.invoices(states=[state], per_state=1)

    paths = [node.args["path"] for node in creates(eks.plan(looped)).values()]
    assert paths == [
        "tenant:harman",
        "tenant:harman/bank_account:kuveyt",
        "tenant:harman/bank_account:ziraat",
        "tenant:harman/invoices#0",
        "tenant:harman/invoices#1",
    ]


def test_key_built_from_a_ref_is_refused(eks_registry: Registry) -> None:
    register_kinds()

    @eks.seed("refkey", backend="eksenapi")
    def refkey(d: eks.Data):
        bayi = d.federation("anadolu-bayi", type="Reseller")
        d.tenant(f"{bayi.ref.id}", name="T")

    with pytest.raises(TypeError, match="built from a ref"):
        eks.plan(refkey)


def test_underscore_names_are_not_kinds(eks_registry: Registry) -> None:
    register_kinds()
    seen: list[Data] = []

    @eks.seed("private", backend="eksenapi")
    def private(d: eks.Data):
        seen.append(d)
        with pytest.raises(AttributeError):
            d._missing  # noqa: B018
        with pytest.raises(AttributeError):
            d.tenant("t", name="T")._missing  # noqa: B018

    eks.plan(private)
    assert len(seen) == 1


def test_plugin_kinds_add_their_requirement(eks_registry: Registry) -> None:
    register_kinds()
    with contributing("acme.seeds", "1.4.0"):
        @eks.resource("gold_rate", description="A gold rate of the day")
        class GoldRate(eks.Resource):
            buy: str
            sell: str

    @eks.seed("rates", backend="eksenapi")
    def rates(d: eks.Data):
        d.tenant("harman", name="Harman")
        d.gold_rate("today", buy="2450.10", sell="2460.00")

    plan = eks.plan(rates)
    assert plan.requires == [Requirement(plugin="acme.seeds", version="1.4.0")]
    assert creates(plan)["gold_rate:today"].args["fields"] == {"buy": "2450.10", "sell": "2460.00"}
    del GoldRate


def test_resource_phases_are_recorded(eks_registry: Registry) -> None:
    @eks.resource("schema_patch", phase="schema", description="A schema level change")
    class SchemaPatch(eks.Resource):
        sql: str

    @eks.seed("phased", backend="eksenapi")
    def phased(d: eks.Data):
        d.schema_patch("indexes", sql="CREATE INDEX x ON y (z)")

    (node,) = eks.plan(phased).nodes
    assert node.args["phase"] == "schema"
    del SchemaPatch
