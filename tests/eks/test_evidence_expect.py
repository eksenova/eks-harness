from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import by, expect, t
from eks.assertions import TargetExpectation, ValueExpectation
from eks.ref import Ref, node_of, type_of
from eks.tracing import TraceContext, TraceError, tracing
from eks.tracing import record as trace_record
from eks_harness.ir.ops.check import (
    ExpectTarget,
    ExpectValue,
    HasCount,
    HasText,
    Length,
)
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry
from eks_harness.ir.time import TimeError

COUNT_ROW = {"target": {"by": "test_id", "args": {"id": "row"}}}


def plan_twice(fn: Callable[..., Any]) -> Plan:
    first = eks.plan(fn, platform="web")
    second = eks.plan(fn, platform="web")
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    return first


def ops(plan: Plan, op: str) -> list[Node]:
    return [node for node in plan.nodes if node.op == op]


def test_design_lines_record_exact_args() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def subeler(s: eks.Session) -> None:
        seen["lease"] = s.resources["lease"]
        seen["visible"] = expect(by.text("Kadıköy")).visible()
        seen["count"] = expect(by.test_id("invoice-row")).count(at_least=1)

    plan = plan_twice(subeler)
    visible, count = plan.nodes
    assert visible.op == "expect.target"
    assert visible.args == {
        "target": {"by": "text", "args": {"text": "Kadıköy", "exact": True}},
        "assertion": {"kind": "visible"},
        "soft": False,
        "eventually": False,
    }
    assert count.args == {
        "target": {"by": "test_id", "args": {"id": "invoice-row"}},
        "assertion": {"kind": "count", "at_least": 1},
        "soft": False,
        "eventually": False,
    }
    # expect.target runs on the session's lease, in program order
    assert visible.uses == [seen["lease"]]
    assert count.uses == [seen["lease"]]
    assert count.after == [visible.id]
    assert visible.out == "json"
    assert node_of(seen["visible"]) == visible.id
    assert type_of(seen["count"]) == "json"
    # the args validate against the op model and round-trip
    model = ExpectTarget.model_validate(count.args)
    assert isinstance(model.assertion, HasCount)


def test_target_assertion_variants() -> None:
    @eks.flow(app="web-app")
    def variants(s: eks.Session) -> None:
        expect("#save").hidden()
        expect("#save").enabled()
        expect("#save").disabled()
        expect("#agree").checked()
        expect("#agree").checked(False)
        expect("#title").text("Faturalar")
        expect("#title").text(contains="Fatura", matches="^F")
        expect("#amount").value("5")
        expect("#amount").value(matches=r"\d+")
        expect("#link").attr("href", contains="/invoice")
        expect("#link").attr("aria-busy", "false")
        expect(".row").count(3)
        expect(".row").count(at_least=1, at_most=10)
        expect(".row").count(0)

    plan = plan_twice(variants)
    assertions = [node.args["assertion"] for node in plan.nodes]
    assert assertions == [
        {"kind": "hidden"},
        {"kind": "enabled"},
        {"kind": "disabled"},
        {"kind": "checked", "on": True},
        {"kind": "checked", "on": False},
        {"kind": "text", "equals": "Faturalar"},
        {"kind": "text", "contains": "Fatura", "matches": "^F"},
        {"kind": "value", "equals": "5"},
        {"kind": "value", "matches": r"\d+"},
        {"kind": "attr", "name": "href", "contains": "/invoice"},
        {"kind": "attr", "name": "aria-busy", "equals": "false"},
        {"kind": "count", "equals": 3},
        {"kind": "count", "at_least": 1, "at_most": 10},
        {"kind": "count", "equals": 0},
    ]
    assert [node.args["target"] for node in plan.nodes[:3]] == [{"by": "test_id", "args": {"id": "save"}}] * 3
    # a bare string is text
    assert plan.nodes[-1].args["target"] == {"by": "text", "args": {"text": ".row", "exact": True}}


def test_text_assertions_accept_refs_and_fstrings() -> None:
    @eks.flow(app="web-app")
    def refs(s: eks.Session) -> None:
        name = s.record_op("ui.text", {"target": {"by": "test_id", "args": {"id": "name"}}})
        expect("#title").text(name)
        expect("#title").text(contains=f"Merhaba {name}!")

    plan = plan_twice(refs)
    read, equals, contains = plan.nodes
    assert equals.args["assertion"] == {"kind": "text", "equals": {"$ref": read.id, "path": []}}
    assert contains.args["assertion"] == {
        "kind": "text",
        "contains": {"$fmt": ["Merhaba ", {"$ref": read.id, "path": []}, "!"]},
    }
    # the referenced read becomes an ordering edge
    assert read.id in contains.after
    assert isinstance(ExpectTarget.model_validate(equals.args).assertion, HasText)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: expect("#x").text(), "at least one of equals"),
        (lambda: expect("#x").value(), "at least one of equals"),
        (lambda: expect("#x").attr("href"), "at least one of equals"),
        (lambda: expect("#x").count(), "needs a bound"),
        (lambda: expect("#x").count(equals=True), "takes an int"),
        (lambda: expect("#x").count(at_least=1.5), "takes an int"),  # type: ignore[arg-type]
        (lambda: expect("#x").attr("", "x"), "attribute name"),
        (lambda: expect("#x").checked("yes"), "True or False"),  # type: ignore[arg-type]
        (lambda: expect("#x").text(matches=5), "pattern string"),  # type: ignore[arg-type]
        (lambda: expect("#x").visible(message=3), "plain string"),  # type: ignore[arg-type]
        (lambda: expect("#x").text(5), r"expect\(\.\.\.\)\.text\(\) equals must be a string or a Ref"),  # type: ignore[arg-type]
        (lambda: expect("#x").value(contains=7), r"\.value\(\) contains= must be a string or a Ref"),  # type: ignore[arg-type]
        (lambda: expect("#x").attr("href", 1), r"\.attr\(\) equals must be a string or a Ref"),  # type: ignore[arg-type]
        (lambda: expect(".row").count(at_least=5, at_most=2), "at_least=5 is above at_most=2"),
        (lambda: expect(".row").count(1, at_least=2), "equals 1 lies outside"),
        (lambda: expect(".row").count(9, at_most=4), "equals 9 lies outside"),
    ],
)
def test_target_argument_errors(call: Callable[[], object], message: str) -> None:
    @eks.flow(app="web-app")
    def broken(s: eks.Session) -> None:
        call()

    with pytest.raises(TypeError, match=message):
        eks.plan(broken, platform="web")


def test_negative_count_is_rejected_by_the_model() -> None:
    @eks.flow(app="web-app")
    def negative(s: eks.Session) -> None:
        expect("#x").count(at_least=-1)

    with pytest.raises(ValidationError):
        eks.plan(negative, platform="web")


def test_expect_rejects_other_subjects() -> None:
    with pytest.raises(TypeError, match="takes a target"):
        expect(42)  # type: ignore[call-overload]
    with pytest.raises(TypeError, match="takes a target"):
        expect.soft(None)  # type: ignore[call-overload]


def test_dispatch_by_subject_type() -> None:
    assert isinstance(expect("#x"), TargetExpectation)
    assert isinstance(expect(by.role("button")), TargetExpectation)
    assert isinstance(expect(Ref("n0000000000", "int")), ValueExpectation)
    assert isinstance(expect.eventually(Ref("n0000000000", "int")), ValueExpectation)


def test_soft_eventually_timeout_and_message() -> None:
    @eks.flow(app="web-app")
    def options(s: eks.Session) -> None:
        expect.soft(by.text("Kaydedildi")).visible()
        expect.eventually(by.test_id("toast"), timeout=t("10s")).hidden()
        expect.eventually("#spinner").hidden(message="spinner kalktı")
        count = s.record_op("ui.count", COUNT_ROW)
        expect.soft(count).ge(1)
        expect.eventually(count, timeout=t("5s")).eq(3)

    plan = plan_twice(options)
    soft, eventual, untimed, _, soft_value, eventual_value = plan.nodes
    assert soft.args["soft"] is True
    assert soft.args["eventually"] is False
    assert "timeout" not in soft.args
    assert eventual.args["eventually"] is True
    assert eventual.args["soft"] is False
    assert eventual.args["timeout"] == "10s"
    assert untimed.args == {
        "target": {"by": "test_id", "args": {"id": "spinner"}},
        "assertion": {"kind": "hidden"},
        "soft": False,
        "eventually": True,
        "message": "spinner kalktı",
    }
    assert soft_value.args["soft"] is True
    assert eventual_value.args["eventually"] is True
    assert eventual_value.args["timeout"] == "5s"


def test_eventually_timeout_must_be_a_time() -> None:
    with pytest.raises(TypeError, match="expects a Time"):
        expect.eventually("#x", timeout="10s")  # type: ignore[call-overload]
    with pytest.raises(TypeError, match="bare numbers"):
        expect.eventually("#x", timeout=10)  # type: ignore[call-overload]
    with pytest.raises(TimeError, match="duration"):
        expect.eventually("#x", timeout=t("end"))


def test_target_expectations_override_the_session_timeout_per_op() -> None:
    # design 4.6: session defaults (timeouts) can be overridden per op, so expect(target) and
    # expect.soft(target) take a timeout of their own; the expectation does not become eventually.
    @eks.flow(app="web-app")
    def waits(s: eks.Session) -> None:
        expect(by.text("Kaydedildi"), timeout=t("8s")).visible()
        expect.soft("#banner", timeout=t("2s")).hidden()

    plan = plan_twice(waits)
    hard, soft = plan.nodes
    assert hard.args["timeout"] == "8s"
    assert hard.args["eventually"] is False
    assert hard.args["soft"] is False
    assert soft.args["timeout"] == "2s"
    assert soft.args["soft"] is True


def test_value_expectations_reject_a_wait_timeout() -> None:
    count = Ref("n0000000000", "int")
    with pytest.raises(TypeError, match=r"expect\(ref, timeout"):
        expect(count, timeout=t("2s"))  # type: ignore[call-overload]
    with pytest.raises(TypeError, match=r"expect\.soft\(ref, timeout"):
        expect.soft(count, timeout=t("2s"))  # type: ignore[call-overload]
    with pytest.raises(TypeError, match="bare numbers"):
        expect("#x", timeout=2)  # type: ignore[call-overload]
    with pytest.raises(TimeError, match="duration"):
        expect.soft("#x", timeout=t("beat:2"))


def test_value_expectations_record_expect_value() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def values(s: eks.Session) -> None:
        count = s.record_op("ui.count", COUNT_ROW)
        rows = s.record_op("ui.state", {"path": "invoices.items"})
        seen["eq"] = expect(count).eq(5)
        expect(count).ne(0)
        expect(rows).contains({"id": 7})
        expect(rows[0].number).matches(r"^EKS\d+$")
        expect(count).gt(1)
        expect(count).lt(100)
        expect(count).le(count)
        expect(rows.first.paid).truthy()
        expect(rows.first.cancelled).falsy(message="iptal edilmemeli")
        expect(rows).length(at_least=1)
        expect(rows).length(3)

    plan = plan_twice(values)
    count, rows, *checks = plan.nodes
    assert all(node.op == "expect.value" for node in checks)
    # expect.value takes no resources: the value is already produced
    assert all(node.uses == [] for node in checks)
    count_ref = {"$ref": count.id, "path": []}
    assert checks[0].args == {
        "value": count_ref,
        "assertion": {"kind": "eq", "value": 5},
        "soft": False,
        "eventually": False,
    }
    assert node_of(seen["eq"]) == checks[0].id
    assert [node.args["assertion"] for node in checks[1:]] == [
        {"kind": "ne", "value": 0},
        {"kind": "contains", "value": {"id": 7}},
        {"kind": "matches", "pattern": r"^EKS\d+$"},
        {"kind": "gt", "value": 1},
        {"kind": "lt", "value": 100},
        {"kind": "le", "value": count_ref},
        {"kind": "truthy"},
        {"kind": "falsy"},
        {"kind": "length", "at_least": 1},
        {"kind": "length", "equals": 3},
    ]
    assert checks[3].args["value"] == {"$ref": rows.id, "path": [0, "number"]}
    assert checks[8].args["message"] == "iptal edilmemeli"
    # every expect.value depends on the producer it reads
    assert all(count.id in node.after or rows.id in node.after for node in checks)
    for node in checks:
        ExpectValue.model_validate(node.args)


def test_value_argument_errors() -> None:
    @eks.flow(app="web-app")
    def none_value(s: eks.Session) -> None:
        expect(s.record_op("ui.count", COUNT_ROW)).eq(None)

    with pytest.raises(TypeError, match="falsy"):
        eks.plan(none_value, platform="web")

    @eks.flow(app="web-app")
    def unbounded(s: eks.Session) -> None:
        expect(s.record_op("ui.count", COUNT_ROW)).length()

    with pytest.raises(TypeError, match="needs a bound"):
        eks.plan(unbounded, platform="web")

    @eks.flow(app="web-app")
    def pattern(s: eks.Session) -> None:
        expect(s.record_op("ui.count", COUNT_ROW)).matches(5)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="pattern string"):
        eks.plan(pattern, platform="web")


def test_value_expectation_without_session_uses_the_trace(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="no-session", registry=eks_registry)
    with tracing(ctx):
        count = trace_record("ui.count", COUNT_ROW)
        checked = expect(count).gt(0)
    plan = ctx.finalize()
    read, node = plan.nodes
    assert node.op == "expect.value"
    assert node.uses == []
    assert node.args["value"] == {"$ref": read.id, "path": []}
    assert node_of(checked) == node.id


def test_target_expectation_needs_a_session(eks_registry: Registry) -> None:
    with pytest.raises(TraceError, match="no active session"):
        expect(by.text("Kadıköy")).visible()
    with pytest.raises(TraceError, match="eks.plan"):
        expect(Ref("n0000000000", "int")).eq(5)
    ctx = TraceContext(kind="edit", name="no-session", registry=eks_registry)
    with tracing(ctx), pytest.raises(TraceError, match="no active session"):
        expect("#x").visible()



def test_bounds_that_agree_are_kept() -> None:
    @eks.flow(app="web-app")
    def agreeing(s: eks.Session) -> None:
        expect(".row").count(3, at_least=1, at_most=5)
        expect(".row").count(at_least=2, at_most=2)

    plan = plan_twice(agreeing)
    assert [node.args["assertion"] for node in plan.nodes] == [
        {"kind": "count", "equals": 3, "at_least": 1, "at_most": 5},
        {"kind": "count", "at_least": 2, "at_most": 2},
    ]


@pytest.mark.parametrize(
    "call",
    [
        lambda rows: expect(rows).length(at_least=3, at_most=1),
        lambda rows: expect(rows).length(0, at_least=1),
        lambda rows: expect(rows).length(5, at_most=2),
    ],
)
def test_contradicting_length_bounds(call: Callable[[Ref[Any]], object]) -> None:
    @eks.flow(app="web-app")
    def contradicting(s: eks.Session) -> None:
        call(s.record_op("ui.state", {"path": "invoices.items"}))

    with pytest.raises(TypeError, match="can never pass"):
        eks.plan(contradicting, platform="web")


@pytest.mark.parametrize("model", [HasCount, Length])
@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "needs a bound"),
        ({"at_least": 5, "at_most": 2}, "above at_most"),
        ({"equals": 1, "at_least": 2}, "lies outside"),
        ({"equals": 9, "at_most": 4}, "lies outside"),
    ],
)
def test_bounded_models_reject_impossible_bounds(
    model: type[HasCount] | type[Length], data: dict[str, int], message: str
) -> None:
    # IR loaded from JSON goes through the same check as the builders
    with pytest.raises(ValidationError, match=message):
        model.model_validate(data)
    assert model.model_validate({"equals": 2, "at_least": 1, "at_most": 3}).equals == 2


def test_impossible_count_in_loaded_ir_is_rejected() -> None:
    with pytest.raises(ValidationError, match="above at_most"):
        ExpectTarget.model_validate({
            "target": {"by": "css", "args": {"selector": ".row"}},
            "assertion": {"kind": "count", "at_least": 5, "at_most": 2},
        })


def test_eventually_needs_an_idempotent_producer() -> None:
    @eks.flow(app="web-app")
    def posted(s: eks.Session) -> None:
        created = s.record_op("api.post", {"path": "/api/invoices", "json": {"amount": 5}})
        expect.eventually(created, timeout=t("5s")).contains({"status": "ok"})

    with pytest.raises(TraceError, match=r"'api.post' is not idempotent.*s\.ui\.text"):
        eks.plan(posted, platform="web")

    @eks.flow(app="web-app")
    def tapped(s: eks.Session) -> None:
        clicked = s.record_op("ui.tap", {"target": {"by": "test_id", "args": {"id": "save"}}})
        expect.eventually(clicked).truthy()

    with pytest.raises(TraceError, match="'ui.tap' is not idempotent"):
        eks.plan(tapped, platform="web")

    # plain and soft expectations check a non-idempotent value once, which is fine
    @eks.flow(app="web-app")
    def once(s: eks.Session) -> None:
        created = s.record_op("api.post", {"path": "/api/invoices", "json": {"amount": 5}})
        expect(created).contains({"status": "ok"})
        expect.soft(created).truthy()

    assert [node.op for node in plan_twice(once).nodes] == ["api.post", "expect.value", "expect.value"]


def test_eventually_on_idempotent_reads_and_derived_paths() -> None:
    @eks.flow(app="web-app")
    def reads(s: eks.Session) -> None:
        listed = s.record_op("api.get", {"path": "/api/invoices"})
        state = s.record_op("ui.state", {"path": "invoices"})
        expect.eventually(listed).length(at_least=1)
        expect.eventually(state.items[0].paid).truthy()

    plan = plan_twice(reads)
    assert [node.args["eventually"] for node in plan.nodes[2:]] == [True, True]


def test_eventually_without_a_session_checks_the_producer(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="no-session", registry=eks_registry)
    with tracing(ctx):
        count = trace_record("ui.count", COUNT_ROW)
        expect.eventually(count).gt(0)
        tapped = trace_record("ui.tap", COUNT_ROW)
        with pytest.raises(TraceError, match="'ui.tap' is not idempotent"):
            expect.eventually(tapped).truthy()


@pytest.mark.parametrize(
    "assertion",
    [{"kind": "text"}, {"kind": "value"}, {"kind": "attr", "name": "href"}],
)
def test_text_like_models_need_a_criterion(assertion: dict[str, str]) -> None:
    # IR loaded from JSON or sent through s.op must carry equals, contains or matches
    with pytest.raises(ValidationError, match="needs at least one of equals, contains or matches"):
        ExpectTarget.model_validate({"target": {"by": "css", "args": {"selector": ".row"}}, "assertion": assertion})
    loaded = ExpectTarget.model_validate({
        "target": {"by": "css", "args": {"selector": ".row"}},
        "assertion": {**assertion, "contains": "Kayd"},
    })
    assert loaded.assertion.kind == assertion["kind"]
    with pytest.raises(ValidationError, match="needs at least one"):
        HasText()


def test_eventually_on_a_runtime_loop_item_points_to_the_source_read() -> None:
    @eks.flow(app="web-app")
    def looped(s: eks.Session) -> None:
        rows = s.record_op("api.get", {"path": "/api/invoices"})
        with eks.each(rows) as item:
            expect.eventually(item.paid).truthy()

    with pytest.raises(TraceError, match=r"snapshot of its list from 'api.get'.*on the read that produced the list"):
        eks.plan(looped, platform="web")

    # a plain expectation on the item checks it once, which is fine
    @eks.flow(app="web-app")
    def checked(s: eks.Session) -> None:
        rows = s.record_op("api.get", {"path": "/api/invoices"})
        with eks.each(rows) as item:
            expect(item.paid).truthy()

    plan = plan_twice(checked)
    assert [node.op for node in plan.nodes] == ["api.get", "ctl.each"]


def test_eventually_on_a_literal_loop_item_is_a_constant() -> None:
    @eks.flow(app="web-app")
    def looped(s: eks.Session) -> None:
        with eks.each([1, 2]) as item:
            expect.eventually(item).gt(0)

    with pytest.raises(TraceError, match="literal eks.each: it is a constant"):
        eks.plan(looped, platform="web")


@pytest.mark.parametrize(
    ("assertion", "message"),
    [
        (lambda: expect("#x").text(matches="(["), r"text\(\) matches= '\(\[' is not a valid regular expression"),
        (lambda: expect("#x").value(matches="a{2,1}"), r"value\(\) matches= 'a\{2,1\}' is not a valid regular"),
        (lambda: expect("#x").attr("href", matches="*x"), r"attr\(\) matches= '\*x' is not a valid regular"),
    ],
)
def test_target_patterns_are_compiled_at_plan_time(assertion: Callable[[], object], message: str) -> None:
    @eks.flow(app="web-app")
    def broken(s: eks.Session) -> None:
        assertion()

    with pytest.raises(TypeError, match=message):
        eks.plan(broken, platform="web")


def test_value_pattern_is_compiled_at_plan_time(eks_registry: Registry) -> None:
    @eks.flow(app="web-app")
    def broken(s: eks.Session) -> None:
        expect(s.record_op("ui.count", COUNT_ROW)).matches("([")

    with pytest.raises(TypeError, match=r"expect\(ref\)\.matches\(\) '\(\[' is not a valid regular expression"):
        eks.plan(broken, platform="web")

    # outside a session the pattern is checked the same way
    with tracing(TraceContext(kind="edit", name="no-session", registry=eks_registry)):
        produced = trace_record("ui.count", COUNT_ROW)
        with pytest.raises(TypeError, match="is not a valid regular expression"):
            expect(produced).matches("a(")


@pytest.mark.parametrize(
    ("assertion", "message"),
    [
        (
            lambda r: expect(by.text("x")).text(matches=f"^{r}$"),
            (
                r"expect\(\.\.\.\)\.text\(\) matches= must be plain text; refs cannot go into a pattern, "
                r"use equals=ref or contains=ref"
            ),
        ),
        (
            lambda r: expect("#x").value(matches=f"{r}"),
            r"value\(\) matches= must be plain text",
        ),
        (
            lambda r: expect("#x").attr("href", matches=f"/{r}"),
            r"attr\(\) matches= must be plain text",
        ),
        (
            lambda r: expect(r).matches(f"^{r}$"),
            r"expect\(ref\)\.matches\(\) must be plain text; refs cannot go into a pattern, use \.eq\(ref\)",
        ),
        (
            lambda r: expect(by.text("x")).visible(message=f"got {r}"),
            r"expect message= must be plain text; refs cannot go into a message",
        ),
        (
            lambda r: expect(r).eq(3, message=f"got {r}"),
            r"expect message= must be plain text",
        ),
    ],
)
def test_refs_in_patterns_and_messages_are_named_errors(
    assertion: Callable[[Ref[Any]], object], message: str
) -> None:
    @eks.flow(app="web-app")
    def broken(s: eks.Session) -> None:
        assertion(s.record_op("ui.count", COUNT_ROW))

    with pytest.raises(TypeError, match=message):
        eks.plan(broken, platform="web")
