from __future__ import annotations

from typing import Annotated, Any

import pytest

import eks
from eks.tracing import TraceError
from eks_harness.engine.validator import ISSUE_CODES, Report
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry

NOW = "2026-01-15T09:00+03:00"


def codes(report: Report) -> list[tuple[str, str | None]]:
    # plan.hash is reported for every hand edit; these tests look at the rules behind it
    return [(issue.code, issue.field) for issue in report.issues if issue.code != "plan.hash"]


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def register_resources() -> None:
    @eks.resource("federation")
    class Federation(eks.Resource):
        type: str

    @eks.resource("tenant")
    class Tenant(eks.Resource):
        name: str
        reseller: Annotated[eks.ResourceRef, eks.refers_to("federation")] | None = None
        anything: eks.ResourceRef | None = None

    @eks.resource("bank_account", parent="tenant")
    class BankAccount(eks.Resource):
        currency: str

    @eks.resource("ledger", parent="tenant", phase="finalize", keyed=False)
    class Ledger(eks.Resource):
        account: Annotated[eks.ResourceRef, eks.refers_to("bank_account")] | None = None


def seed_plan() -> Plan:
    register_resources()

    @eks.seed("standard", backend="eksenapi", now=NOW)
    def standard(d: Any) -> None:
        bayi = d.federation("bayi", type="Reseller")
        harman = d.tenant("harman", name="Harman", reseller=bayi)
        harman.bank_account("kuveyt", currency="TRY")
        harman.ledger()

    return eks.plan(standard)


def test_a_traced_seed_validates() -> None:
    assert eks.validate(seed_plan()).issues == ()


def test_reference_fields_accept_only_their_kind() -> None:
    register_resources()

    @eks.seed("wrong", backend="eksenapi", now=NOW)
    def wrong(d: Any) -> None:
        harman = d.tenant("harman", name="Harman")
        account = harman.bank_account("kuveyt", currency="TRY")
        d.tenant("other", name="Other", reseller=account)

    with pytest.raises(TraceError, match="refers to a federation resource"):
        eks.plan(wrong)

    @eks.seed("loose", backend="eksenapi", now=NOW)
    def loose(d: Any) -> None:
        harman = d.tenant("harman", name="Harman")
        account = harman.bank_account("kuveyt", currency="TRY")
        # a bare eks.ResourceRef accepts any kind
        d.tenant("other", name="Other", anything=account)

    assert eks.validate(eks.plan(loose)).issues == ()


def test_hand_edited_references_are_typed() -> None:
    plan = seed_plan()
    _federation, tenant, _account, ledger = nodes_of(plan, "data.create")
    ledger.args["fields"] = {"account": {"$ref": tenant.id, "path": []}}
    assert codes(eks.validate(plan)) == [("ref.type", "fields.account")]


def test_unknown_kinds_fields_parents_and_phases() -> None:
    plan = seed_plan()
    federation, tenant, account, ledger = nodes_of(plan, "data.create")
    federation.args["kind"] = "nope"
    federation.out = "resource:nope"
    account.args["fields"] = {"currency": 5, "bogus": True}
    account.args["parent"] = {"$ref": federation.id, "path": []}
    ledger.args["phase"] = "api"
    ledger.args["key"] = "main"
    tenant.args["parent"] = {"$ref": federation.id, "path": []}
    report = eks.validate(plan)
    assert [found for found in codes(report) if found[0] != "seed.path"] == [
        ("resource.kind_unknown", "kind"),
        ("ref.type", "fields.reseller"),
        ("resource.parent", "parent"),
        ("resource.fields", "fields"),
        ("resource.parent", "parent"),
        ("resource.fields", "key"),
        ("resource.phase", "phase"),
    ]
    # the renamed kind, the moved parents and the added key change the paths those nodes would get
    moved = [issue.node for issue in report.issues if issue.code == "seed.path"]
    assert moved == [federation.id, tenant.id, account.id, ledger.id]


def web_flow() -> Plan:
    @eks.macro(namespace="shop", platforms=["web"])
    def login(s: Any, user: str, remember: bool = False) -> None:
        s.ui.fill("#user", user)

    @eks.flow(app="web-app", platform="web", now=NOW, timezone="Europe/Istanbul", locale="tr-TR")
    def shopping(s: Any) -> None:
        s.shop.login("ayse")
        s.ui.tap("#buy")

    return eks.plan(shopping)


def test_session_and_resources_agree() -> None:
    plan = web_flow()
    assert eks.validate(plan).issues == ()
    assert plan.session is not None
    moved = plan.model_copy(update={"session": plan.session.model_copy(update={"platform": "ios"})})
    found = [issue for issue in eks.validate(moved).issues if issue.code == "session.mismatch"]
    assert len(found) == 1
    assert "lease.web" in found[0].message
    renamed = plan.model_copy(update={"session": plan.session.model_copy(update={"app": "mobile-app"})})
    # the flow holds only its lease (no seed, backend or persona), so only the lease disagrees with the session
    assert [issue.code for issue in eks.validate(renamed).issues].count("session.mismatch") == 1


def test_macro_nodes_are_checked_against_the_registry() -> None:
    plan = web_flow()
    (macro,) = nodes_of(plan, "ctl.macro")
    assert macro.args == {"name": "shop.login", "version": "1", "params": {"user": "ayse", "remember": False}}
    macro.args["version"] = "2"
    macro.args["params"] = {"remember": "yes", "bogus": 1}
    assert codes(eks.validate(plan)) == [("macro.params", "params"), ("macro.params", "params"),
                                         ("macro.version", "version")]
    macro.args["name"] = "m1.nope"
    assert codes(eks.validate(plan)) == [("macro.unknown", "name")]


def test_macro_platforms_are_checked() -> None:
    plan = web_flow()
    (macro,) = nodes_of(plan, "ctl.macro")
    assert plan.session is not None
    ios = plan.session.model_copy(update={"platform": "ios"})
    found = {issue.code for issue in eks.validate(plan.model_copy(update={"session": ios})).issues
             if issue.node == macro.id}
    assert "macro.platform" in found


def test_new_codes_are_documented(eks_registry: Registry) -> None:
    for code in ("resource.kind_unknown", "resource.fields", "resource.parent", "resource.phase",
                 "session.mismatch", "macro.unknown", "macro.version", "macro.params", "macro.platform"):
        assert code in ISSUE_CODES
