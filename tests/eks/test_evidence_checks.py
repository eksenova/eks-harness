from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import eks
from eks import by, t
from eks.checks import to_use, use
from eks.ref import Ref, type_of
from eks_harness.ir.ops.check import CheckUse, ConsoleErrors, PerfBudget, VisualGolden
from eks_harness.ir.ops.debug import Break
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry, RegistryError

COUNT_ROW = {"target": {"by": "test_id", "args": {"id": "row"}}}


def plan_twice(fn: Callable[..., Any]) -> Plan:
    first = eks.plan(fn, platform="web")
    second = eks.plan(fn, platform="web")
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    return first


def test_use_validates_and_dumps_params() -> None:
    assert use("visual", threshold=0.01) == CheckUse(kind="visual", params={"threshold": 0.01, "masks": []})
    assert use("console_errors") == CheckUse(kind="console_errors", params={"noise": []})
    assert use("failed_requests", allow_status=[401, 404]).params == {"noise": [], "allow_status": [401, 404]}
    # targets and times are encoded to their JSON forms
    assert use("visual", masks=[by.test_id("clock"), by.css(".avatar")]).params["masks"] == [
        {"by": "test_id", "args": {"id": "clock"}},
        {"by": "css", "args": {"selector": ".avatar"}},
    ]
    assert use("perf", lcp=t("2.5s"), requests=40).params == {"lcp": "2.5s", "requests": 40}
    # the eks namespace exposes the same function
    assert eks.checks.use("route", expected="/invoices") == CheckUse(kind="route", params={"expected": "/invoices"})


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda: use("nope"), RegistryError, "nope"),
        (lambda: use("visua"), RegistryError, "visual"),
        (lambda: use("visual", threshold=2), ValidationError, "threshold"),
        (lambda: use("visual", bogus=1), ValidationError, "bogus"),
        (lambda: use("route"), ValidationError, "expected"),
        (lambda: use("perf", lcp=2.5), ValidationError, "lcp"),
        (lambda: use(""), TypeError, "non-empty string"),
        (lambda: use(5), TypeError, "non-empty string"),  # type: ignore[arg-type]
    ],
)
def test_use_errors(call: Callable[[], object], error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        call()


def test_to_use_of_each_input_form() -> None:
    assert to_use("console_errors") == CheckUse(kind="console_errors", params={"noise": []})
    # a CheckUse is revalidated and normalized
    assert to_use(CheckUse(kind="visual", params={"threshold": 0.5})) == CheckUse(
        kind="visual", params={"threshold": 0.5, "masks": []})
    already = use("visual", threshold=0.01)
    assert to_use(already) == already
    # a registered params model instance names its check
    assert to_use(PerfBudget(lcp=t("2.5s"))) == CheckUse(kind="perf", params={"lcp": "2.5s"})
    assert to_use(VisualGolden(masks=(by.text("Saat"),))) == CheckUse(
        kind="visual", params={"threshold": 0.001, "masks": [{"by": "text", "args": {"text": "Saat", "exact": True}}]})
    assert to_use(ConsoleErrors(noise=("favicon",))) == CheckUse(kind="console_errors", params={"noise": ["favicon"]})


def test_to_use_errors() -> None:
    class Loose(BaseModel):
        x: int = 1

    with pytest.raises(RegistryError, match="Loose is not the params model"):
        to_use(Loose())
    with pytest.raises(TypeError, match="a check is"):
        to_use(5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        to_use(CheckUse(kind="visual", params={"threshold": 5}))
    with pytest.raises(RegistryError, match="nope"):
        to_use(CheckUse(kind="nope"))


def test_to_use_takes_an_explicit_registry() -> None:
    empty = Registry()
    with pytest.raises(RegistryError, match="console_errors"):
        to_use("console_errors", empty)
    with pytest.raises(RegistryError, match="PerfBudget"):
        to_use(PerfBudget(), empty)


def test_plugin_checks() -> None:
    @eks.check("contrast", platforms=["web"], description="Text contrast ratio")
    class Contrast(eks.Args):
        ratio: float = 4.5

    assert to_use(Contrast(ratio=7.0)) == CheckUse(kind="contrast", params={"ratio": 7.0})
    assert use("contrast") == CheckUse(kind="contrast", params={"ratio": 4.5})

    eks.check("contrast_strict", platforms=["web"])(Contrast)
    with pytest.raises(RegistryError, match="several checks"):
        to_use(Contrast())

    @eks.flow(app="web-app")
    def plugin(s: eks.Session) -> None:
        s.checkpoint("renk", checks=[eks.checks.use("contrast_strict", ratio=7)])

    plan = plan_twice(plugin)
    assert plan.nodes[0].args["checks"] == [{"kind": "contrast_strict", "params": {"ratio": 7.0}}]


def test_check_params_may_hold_refs() -> None:
    @eks.flow(app="web-app")
    def routed(s: eks.Session) -> None:
        invoice = s.record_op("ui.text", {"target": {"by": "test_id", "args": {"id": "invoice-id"}}})
        s.checkpoint("fatura", checks=[eks.checks.use("route", expected=f"/tenant/invoice/{invoice}")])

    plan = plan_twice(routed)
    read, checkpoint = plan.nodes
    assert checkpoint.args["checks"] == [
        {"kind": "route", "params": {"expected": {"$fmt": ["/tenant/invoice/", {"$ref": read.id, "path": []}]}}},
    ]
    assert read.id in checkpoint.after


def test_debug_break_and_pause_on_error() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def debugging(s: eks.Session) -> None:
        count = s.record_op("ui.state", {"path": "invoices.items"})
        seen["break"] = s.debug.break_("before-submit")
        s.debug.break_("has-rows", when=count.first)
        s.debug.pause_on_error()
        s.debug.pause_on_error(False)

    plan = plan_twice(debugging)
    count, plain, conditional, pause, unpause = plan.nodes
    assert plain.op == "debug.break"
    assert plain.args == {"label": "before-submit"}
    assert plain.uses == []
    assert plain.out == "none"
    assert type_of(seen["break"]) == "none"
    assert conditional.args == {"label": "has-rows", "when": {"$ref": count.id, "path": [0]}}
    assert count.id in conditional.after
    assert Break.model_validate(conditional.args).when is not None
    assert pause.op == "debug.pause_on_error"
    assert pause.args == {"on": True}
    assert unpause.args == {"on": False}
    # breakpoints keep their place in program order
    assert plain.after == [count.id]
    assert pause.after == [conditional.id]


def test_debug_errors() -> None:
    @eks.flow(app="web-app")
    def bad_label(s: eks.Session) -> None:
        s.debug.break_(5)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="plain string"):
        eks.plan(bad_label, platform="web")

    @eks.flow(app="web-app")
    def empty_label(s: eks.Session) -> None:
        s.debug.break_("")

    with pytest.raises(ValidationError):
        eks.plan(empty_label, platform="web")

    @eks.flow(app="web-app")
    def ref_label(s: eks.Session) -> None:
        count = s.record_op("ui.count", COUNT_ROW)
        s.debug.break_(f"rows {count}")

    with pytest.raises(ValidationError):
        eks.plan(ref_label, platform="web")

    @eks.flow(app="web-app")
    def literal_when(s: eks.Session) -> None:
        s.debug.break_("x", when=True)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Ref produced earlier"):
        eks.plan(literal_when, platform="web")

    @eks.flow(app="web-app")
    def bad_toggle(s: eks.Session) -> None:
        s.debug.pause_on_error("yes")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="True or False"):
        eks.plan(bad_toggle, platform="web")


def test_design_debugging_example() -> None:
    @eks.flow(app="web-app")
    def debugging(s: eks.Session) -> None:
        s.debug.break_("before-submit")

    plan = plan_twice(debugging)
    (node,) = plan.nodes
    assert (node.op, node.args) == ("debug.break", {"label": "before-submit"})
    assert isinstance(Ref(node.id, node.out), Ref)


def test_target_params_take_shorthand_strings() -> None:
    # target-like params go through ensure_target like every other builder argument
    # the '#' shorthand names a test id
    clock = {"by": "test_id", "args": {"id": "clock"}}
    assert use("visual", masks=["#clock"]).params["masks"] == [clock]
    assert use("visual", masks=("#clock", by.test_id("avatar"))).params["masks"] == [
        clock,
        {"by": "test_id", "args": {"id": "avatar"}},
    ]
    assert use("text", text="x", region="#a").params == {
        "text": "x",
        "region": {"by": "test_id", "args": {"id": "a"}},
        "engine": "auto",
        "languages": ["tr-TR", "en-US"],
    }
    assert use("safe_area", region=by.test_id("hero")).params["region"] == {"by": "test_id", "args": {"id": "hero"}}
    # a CheckUse written by hand with shorthand strings is normalized the same way
    assert to_use(CheckUse(kind="visual", params={"masks": ["#clock"]})).params["masks"] == [clock]
    # bad targets name the check and the parameter
    with pytest.raises(TypeError, match=r"check visual masks\[0\]"):
        use("visual", masks=[5])
    with pytest.raises(TypeError, match="check text region"):
        use("text", text="x", region=5)


def test_checkpoint_with_shorthand_masks() -> None:
    @eks.flow(app="web-app")
    def masked(s: eks.Session) -> None:
        s.checkpoint("liste", checks=[eks.checks.use("visual", masks=["#clock"])])

    plan = plan_twice(masked)
    checks = plan.nodes[0].args["checks"]
    assert checks == [{"kind": "visual", "params": {
        "threshold": 0.001, "masks": [{"by": "test_id", "args": {"id": "clock"}}],
    }}]


def test_layout_and_toast_checks_take_targets() -> None:
    spill = use("layout_spill", ignore=["#banner", by.role("dialog")])
    assert spill.params == {"ignore": [{"by": "test_id", "args": {"id": "banner"}},
                                       {"by": "role", "args": {"role": "dialog", "exact": False}}]}
    toasts = use("error_toasts", selector="text=Hata")
    assert toasts.params == {"selector": {"by": "text", "args": {"text": "Hata", "exact": True}}}

    @eks.flow(app="mobile-app", platform="ios")
    def phone(s: eks.Session) -> None:
        s.checkpoint("home", checks=[use("error_toasts", selector=by.css("[role=alert]"))])

    # a CSS selector cannot find toasts on iOS: the single Target model checks it per platform
    report = eks.validate(eks.plan(phone))
    assert any(issue.code == "target.surface" and issue.field == "checks.0.params.selector" for issue in report.errors)


def test_flow_checks_become_session_defaults() -> None:
    @eks.flow(app="web-app", platform="web", checks=[PerfBudget(lcp=t("2s")), "visual"])
    def budgeted(s: eks.Session) -> None:
        s.checkpoint("home")

    plan = plan_twice(budgeted)
    assert plan.session is not None
    assert plan.session.checks == (CheckUse(kind="perf", params={"lcp": "2s"}),
                                   CheckUse(kind="visual", params={"threshold": 0.001, "masks": []}))
    assert not [issue for issue in eks.validate(plan).errors]

    @eks.flow(app="web-app", platform="web", checks=["cut"])
    def media_check(s: eks.Session) -> None:
        s.checkpoint("home")

    # a media check is no checkpoint check
    assert [issue.code for issue in eks.validate(eks.plan(media_check)).errors] == ["check.kind"]
    with pytest.raises(RegistryError, match="checks= takes a list"):
        eks.flow(app="web-app", checks="visual")  # type: ignore[arg-type]
