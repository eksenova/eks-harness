from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import by, t
from eks.discovery import load_file
from eks.ref import Ref, node_of, type_of
from eks.tracing import TraceError
from eks_harness.ir.ops.check import Checkpoint, PerfBudget
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.presets import Pace
from eks_harness.ir.registry import contributing
from eks_harness.ir.time import TimeError


def plan_twice(fn: Callable[..., Any]) -> Plan:
    first = eks.plan(fn, platform="web")
    second = eks.plan(fn, platform="web")
    assert first.to_json() == second.to_json()
    assert first.hash == second.hash
    return first


def body(node: Node) -> list[Node]:
    assert node.branches is not None
    return node.branches["body"]


def test_checkpoints_of_the_design_example() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def subeler(s: eks.Session) -> None:
        seen["lease"] = s.resources["lease"]
        seen["ref"] = s.checkpoint("liste")
        s.checkpoint("liste-telefon", viewport="phone")

    plan = plan_twice(subeler)
    first, second = plan.nodes
    assert first.op == second.op == "check.checkpoint"
    # checks=None leaves the default set to the engine: no checks key
    assert first.args == {"label": "liste", "shot": True, "full": False, "soft": False}
    assert second.args == {"label": "liste-telefon", "viewport": "phone", "shot": True, "full": False, "soft": False}
    assert first.uses == [seen["lease"]]
    assert second.after == [first.id]
    assert type_of(seen["ref"]) == "json"
    assert node_of(seen["ref"]) == first.id


def test_checkpoint_checks_in_every_form() -> None:
    @eks.flow(app="web-app")
    def checked(s: eks.Session) -> None:
        s.checkpoint(
            "odeme",
            checks=["console_errors", eks.checks.use("visual", threshold=0.01), PerfBudget(lcp=t("2.5s"))],
            shot=False,
            full=True,
            soft=True,
        )
        s.checkpoint("bos", checks=[])

    plan = plan_twice(checked)
    full, empty = plan.nodes
    assert full.args == {
        "label": "odeme",
        "checks": [
            {"kind": "console_errors", "params": {"noise": []}},
            {"kind": "visual", "params": {"threshold": 0.01, "masks": []}},
            {"kind": "perf", "params": {"lcp": "2.5s"}},
        ],
        "shot": False,
        "full": True,
        "soft": True,
    }
    # an explicit empty list means no checks at all, unlike None
    assert empty.args["checks"] == []
    model = Checkpoint.model_validate(full.args)
    assert model.checks is not None
    assert [use.kind for use in model.checks] == ["console_errors", "visual", "perf"]


def test_checkpoint_label_with_a_ref() -> None:
    @eks.flow(app="web-app")
    def labelled(s: eks.Session) -> None:
        number = s.record_op("ui.text", {"target": {"by": "test_id", "args": {"id": "number"}}})
        s.checkpoint(f"fatura-{number}")

    plan = plan_twice(labelled)
    read, checkpoint = plan.nodes
    assert checkpoint.args["label"] == {"$fmt": ["fatura-", {"$ref": read.id, "path": []}]}


@pytest.mark.parametrize(
    ("checks", "error", "message"),
    [
        ("visual", TypeError, "takes a list"),
        (["cut"], TraceError, "media family"),
        (["nope"], ValueError, "nope"),
        ([eks.checks.use("route", expected="/x"), 5], TypeError, "a check is"),
    ],
)
def test_checkpoint_check_errors(checks: Any, error: type[Exception], message: str) -> None:
    @eks.flow(app="web-app")
    def broken(s: eks.Session) -> None:
        s.checkpoint("x", checks=checks)

    with pytest.raises(error, match=message):
        eks.plan(broken, platform="web")


def test_shot_dump_and_har() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def evidence(s: eks.Session) -> None:
        seen["lease"] = s.resources["lease"]
        seen["shot"] = s.shot("liste")
        s.shot("toplam", element=by.test_id("total"), focus="#summary", full=True, viewport="phone",
               hide=["#clock", by.css(".avatar")], at=t("1s"))
        s.dump("a11y")
        s.dump("dom", name="sayfa", viewport="desktop")
        s.har(whole=True)
        s.har(name="istekler")

    plan = plan_twice(evidence)
    plain, detailed, a11y, dom, whole, named = plan.nodes
    assert plain.op == "capture.shot"
    assert plain.args == {"name": "liste", "full": False, "hide": []}
    assert plain.out == "artifact"
    assert type_of(seen["shot"]) == "artifact"
    assert plain.pin is None
    assert detailed.args == {
        "name": "toplam",
        "element": {"by": "test_id", "args": {"id": "total"}},
        "focus": {"by": "test_id", "args": {"id": "summary"}},
        "full": True,
        "viewport": "phone",
        # hidden elements are targets like every other element argument
        "hide": [{"by": "test_id", "args": {"id": "clock"}}, {"by": "css", "args": {"selector": ".avatar"}}],
    }
    assert detailed.pin == t("1s")
    assert a11y.op == "capture.dump"
    assert a11y.args == {"kind": "a11y"}
    assert dom.args == {"kind": "dom", "name": "sayfa", "viewport": "desktop"}
    assert whole.op == "capture.har"
    assert whole.args == {"whole": True}
    assert named.args == {"name": "istekler", "whole": False}
    assert all(node.uses == [seen["lease"]] for node in plan.nodes)
    assert all(node.out == "artifact" for node in plan.nodes)


def test_hidden_targets_are_checked_on_the_platform() -> None:
    from eks_harness.engine.validator import validate

    @eks.flow(app="mobile-app", platform="ios")
    def hide_css(s: eks.Session) -> None:
        s.shot("x", hide=[by.css(".avatar")])
        with s.record("tur", hide=["#clock"]):
            s.hold(t("1s"))

    report = validate(eks.plan(hide_css))
    found = [issue for issue in report.issues if issue.code == "target.surface"]
    # css is web only; the test id shorthand inside the recording is fine on ios
    assert len(found) == 1
    assert "'css' is not supported on ios" in found[0].message


def test_shot_and_dump_errors() -> None:
    @eks.flow(app="web-app")
    def hide_string(s: eks.Session) -> None:
        s.shot("x", hide="#clock")

    with pytest.raises(TypeError, match="list of targets"):
        eks.plan(hide_string, platform="web")

    @eks.flow(app="web-app")
    def hide_target(s: eks.Session) -> None:
        s.shot("x", hide=by.test_id("clock"))  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="list of targets"):
        eks.plan(hide_target, platform="web")

    @eks.flow(app="web-app")
    def hide_number(s: eks.Session) -> None:
        s.shot("x", hide=["#clock", 7])  # type: ignore[list-item]

    with pytest.raises(TypeError, match=r"shot hide\[1\]"):
        eks.plan(hide_number, platform="web")

    @eks.flow(app="web-app")
    def string_pin(s: eks.Session) -> None:
        s.shot("x", at="1s")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="expects a Time"):
        eks.plan(string_pin, platform="web")

    @eks.flow(app="web-app")
    def bad_kind(s: eks.Session) -> None:
        s.dump("pdf")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        eks.plan(bad_kind, platform="web")

    @eks.flow(app="web-app")
    def bad_element(s: eks.Session) -> None:
        s.shot("x", element=42)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="shot element"):
        eks.plan(bad_element, platform="web")


def test_record_container() -> None:
    seen: dict[str, Any] = {}

    @eks.flow(app="web-app")
    def demo(s: eks.Session) -> Any:
        seen["lease"] = s.resources["lease"]
        seen["before"] = s.ctx.has_flag("record")
        with s.record("demo", pace="demo") as video:
            seen["inside"] = s.ctx.has_flag("record")
            seen["video"] = video
            s.record_op("ui.tap", {"target": {"by": "test_id", "args": {"id": "create"}}})
            s.hold(t("1s"))
        seen["after"] = s.ctx.has_flag("record")
        s.shot("son")
        return {"video": video}

    plan = plan_twice(demo)
    record, shot = plan.nodes
    assert record.op == "capture.record"
    assert record.args == {"name": "demo", "pace": "demo", "pointer": True, "from_here": True, "hide": []}
    assert record.out == "artifact"
    assert record.uses == [seen["lease"]]
    tap, hold = body(record)
    assert tap.op == "ui.tap"
    assert hold.op == "capture.hold"
    # the first node of the body has no edge to the container; the next one chains to it
    assert tap.after == []
    assert hold.after == [tap.id]
    # the recording flag is set for the body only
    assert seen["before"] is False
    assert seen["inside"] is True
    assert seen["after"] is False
    assert type_of(seen["video"]) == "artifact"
    assert node_of(seen["video"]) == record.id
    assert plan.outputs == {"video": {"$ref": record.id, "path": []}}
    assert shot.after == [record.id]


def test_record_options() -> None:
    pace = Pace(move=t("300ms"), dwell=t("500ms"), type_per_char=t("60ms"), hold=t("1s"), screen_hold=t("2s"),
                press=t("100ms"))

    @eks.flow(app="web-app")
    def options(s: eks.Session) -> None:
        with s.record("tur", pace=pace, viewport="phone", pointer=False, from_here=False, focus="#form", trim=True,
                      hide=["#clock"]):
            s.hold(t("2s"), at=t("1s"), reason="okuma süresi")

    plan = plan_twice(options)
    (record,) = plan.nodes
    assert record.args == {
        "name": "tur",
        # times are stored in canonical form
        "pace": {"move": "0.3s", "dwell": "0.5s", "type_per_char": "0.06s", "hold": "1s", "screen_hold": "2s",
                 "press": "0.1s"},
        "viewport": "phone",
        "pointer": False,
        "from_here": False,
        "focus": {"by": "test_id", "args": {"id": "form"}},
        "trim": True,
        "hide": [{"by": "test_id", "args": {"id": "clock"}}],
    }
    (hold,) = body(record)
    assert hold.args == {"duration": "2s", "reason": "okuma süresi"}
    assert hold.pin == t("1s")


def test_record_errors() -> None:
    @eks.flow(app="web-app")
    def nested(s: eks.Session) -> None:
        with s.record("outer"), s.record("inner"):
            s.hold(t("1s"))

    with pytest.raises(TraceError, match="cannot be nested"):
        eks.plan(nested, platform="web")

    @eks.flow(app="web-app")
    def nested_deeper(s: eks.Session) -> None:
        count = s.record_op("ui.count", {"target": {"by": "test_id", "args": {"id": "row"}}})
        with s.record("outer"), eks.when(count), s.record("inner"):
            s.hold(t("1s"))

    with pytest.raises(TraceError, match="cannot be nested"):
        eks.plan(nested_deeper, platform="web")

    @eks.flow(app="web-app")
    def bad_pace(s: eks.Session) -> None:
        with s.record("x", pace=5):  # type: ignore[arg-type]
            pass

    with pytest.raises(TypeError, match="pace preset name"):
        eks.plan(bad_pace, platform="web")


def test_hold_only_while_recording() -> None:
    @eks.flow(app="web-app")
    def outside(s: eks.Session) -> None:
        s.hold(t("1s"))

    with pytest.raises(TraceError, match="only while recording"):
        eks.plan(outside, platform="web")

    @eks.flow(app="web-app")
    def after_record(s: eks.Session) -> None:
        with s.record("x"):
            s.hold(t("1s"))
        s.hold(t("1s"))

    with pytest.raises(TraceError, match="only while recording"):
        eks.plan(after_record, platform="web")

    @eks.flow(app="web-app")
    def plain_number(s: eks.Session) -> None:
        with s.record("x"):
            s.hold(1)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="bare numbers"):
        eks.plan(plain_number, platform="web")

    @eks.flow(app="web-app")
    def anchored(s: eks.Session) -> None:
        with s.record("x"):
            s.hold(t("bar:2"))

    with pytest.raises(TimeError, match="duration"):
        eks.plan(anchored, platform="web")


def test_record_inside_a_branch_keeps_ids_stable() -> None:
    seen: dict[str, Ref[Any]] = {}

    @eks.flow(app="web-app")
    def stable(s: eks.Session) -> None:
        s.checkpoint("once")
        with s.record("a"):
            seen["shot"] = s.shot("in-a")
        with s.record("b"):
            s.shot("in-b")
        eks.annotate(seen["shot"], [eks.overlay.highlight("#x")])

    plan = plan_twice(stable)
    ids = [node.id for node in plan.nodes]
    assert len(set(ids)) == len(ids)
    first, second = plan.nodes[1], plan.nodes[2]
    assert body(first)[0].id != body(second)[0].id
    annotate = plan.nodes[3]
    assert annotate.args["artifact"] == {"$ref": body(first)[0].id, "path": []}
    assert node_of(seen["shot"]) == body(first)[0].id


EVIDENCE_SOURCE = '''\
import eks
from eks import by, expect, t


@eks.flow(app="web-app")
def evidence(s: eks.Session):
    expect(by.text("Kadıköy")).visible()
    s.checkpoint("liste", checks=["console_errors"])
    with s.record("demo", pace="demo") as video:
        s.hold(t("1s"))
        s.overlay.callout("#total", "Anlık kur", at=t("bar:2"))
    shot = s.shot("liste")
    eks.annotate(shot, [eks.overlay.highlight("#total")])
    s.debug.break_("before-submit")
    return {"video": video}
'''

EVIDENCE_EDITED = '''\
import eks
from eks import by, expect, t


@eks.flow(app="web-app")
def evidence(s: eks.Session):
    # a comment that does not change any id

    expect(by.text("Kadıköy")).visible()
    note = "unrelated statement"

    s.checkpoint("liste", checks=["console_errors"])  # trailing comment
    with s.record("demo", pace="demo") as video:

        s.hold(t("1s"))
        s.overlay.callout(
            "#total",
            "Anlık kur",
            at=t("bar:2")
        )
    shot = s.shot("liste")
    eks.annotate(shot, [eks.overlay.highlight("#total")])
    s.debug.break_("before-submit")
    return {"video": video, "note": note}
'''


def test_evidence_ids_survive_unrelated_edits(tmp_path: Path) -> None:
    path = tmp_path / "evidence.py"
    path.write_text(EVIDENCE_SOURCE, encoding="utf-8")
    (entry,) = load_file(path)
    first = eks.plan(entry, platform="web")
    assert first.to_json() == eks.plan(entry, platform="web").to_json()

    path.write_text(EVIDENCE_EDITED, encoding="utf-8")
    (edited,) = load_file(path)
    second = eks.plan(edited, platform="web")

    assert [node.id for node, _ in first.walk()] == [node.id for node, _ in second.walk()]
    assert [node.hash for node, _ in first.walk()] == [node.hash for node, _ in second.walk()]
    assert [node.op for node, _ in first.walk()] == [
        "expect.target", "check.checkpoint", "capture.record", "capture.hold", "overlay.show", "capture.shot",
        "overlay.annotate", "debug.break",
    ]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda s: s.checkpoint(5), "s.checkpoint label must be a string or a Ref"),
        (lambda s: s.shot(["x"]), "s.shot name must be a string or a Ref"),
        (lambda s: s.dump("a11y", name=3), "s.dump name= must be a string or a Ref"),
        (lambda s: s.har(name=1.5), "s.har name= must be a string or a Ref"),
    ],
)
def test_text_parameters_name_the_builder(call: Callable[[eks.Session], object], message: str) -> None:
    @eks.flow(app="web-app")
    def broken(s: eks.Session) -> None:
        call(s)

    with pytest.raises(TypeError, match=message):
        eks.plan(broken, platform="web")


def test_record_name_must_be_text() -> None:
    @eks.flow(app="web-app")
    def broken(s: eks.Session) -> None:
        with s.record(7):  # type: ignore[arg-type]
            s.hold(t("1s"))

    with pytest.raises(TypeError, match="s.record name must be a string or a Ref"):
        eks.plan(broken, platform="web")


def test_plugin_checks_at_a_checkpoint_are_required_by_the_plan() -> None:
    with contributing("acme.tools", "1.2.0"):

        @eks.check("acme_contrast", platforms=["web"], description="Text contrast ratio")
        class AcmeContrast(eks.Args):
            ratio: float = 4.5

    @eks.flow(app="web-app")
    def contrast(s: eks.Session) -> None:
        s.checkpoint("renk", checks=[eks.checks.use("acme_contrast", ratio=7), AcmeContrast()])

    plan = plan_twice(contrast)
    assert plan.requires == [Requirement(plugin="acme.tools", version="1.2.0")]

    @eks.flow(app="web-app")
    def defaults(s: eks.Session) -> None:
        s.checkpoint("renk")

    # the engine resolves the default set; nothing is required at plan time
    assert plan_twice(defaults).requires == []
