from __future__ import annotations

from collections.abc import Callable
from typing import Any

import eks
from eks import by
from eks.tracing import SessionCore
from eks_harness.engine.validator import ISSUE_CODES, Issue, Report, Validator, validate
from eks_harness.ir.plan import Node, Plan, Resource
from eks_harness.ir.registry import Args, OpSpec, Registry
from eks_harness.ir.target import Platform


def flow_plan(body: Callable[[SessionCore], Any], *, platform: Platform = "web") -> Plan:
    # a frozen now, zone and locale keep the clock.now and clock.zone warnings out of these reports
    @eks.flow(app="web-app", platform=platform, now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul", locale="tr-TR")
    def probe(s: eks.Session) -> Any:
        return body(s)

    return eks.plan(probe)


def basic(s: SessionCore) -> Any:
    count = s.record_op("ui.count", {"target": by.test_id("row")})
    s.record_op("ui.nav", {"route": f"/rows/{count}"})
    s.record_op("ui.tap", {"target": by.text("Kaydet")})
    return {"count": count}


def codes(report: Report) -> list[str]:
    return [issue.code for issue in report.issues]


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def test_valid_flow_has_no_issues() -> None:
    plan = flow_plan(basic)
    report = validate(plan)
    assert report.ok
    assert report.issues == ()
    assert report.errors == ()
    assert report.warnings == ()
    assert report.plan_hash == plan.hash
    assert report.text() == ""


def test_issue_codes_are_documented() -> None:
    # every code this module expects to see is part of the published table
    for code in ("plan.schema", "plan.hash", "node.duplicate", "resource.kind", "resource.unknown", "op.unknown",
                 "op.version", "args.invalid", "node.branches", "node.after", "node.cycle", "control.else",
                 "control.empty"):
        assert code in ISSUE_CODES


def test_plan_schema() -> None:
    plan = flow_plan(basic)
    assert "plan.schema" not in codes(validate(plan))
    plan.schema_ = "eks/plan@2"  # type: ignore[assignment]
    report = validate(plan)
    [issue] = issues_of(report, "plan.schema")
    assert issue.node is None
    assert "eks/plan@2" in issue.message
    assert not report.ok


def test_plan_hash_reports_the_edited_node_only() -> None:
    plan = flow_plan(basic)
    assert "plan.hash" not in codes(validate(plan))
    nav = plan.nodes[1]
    assert nav.op == "ui.nav"
    nav.args["route"] = "/elsewhere"
    report = validate(plan)
    found = issues_of(report, "plan.hash")
    assert [issue.node for issue in found] == [nav.id]
    assert nav.src is not None
    assert found[0].src == nav.src


def test_plan_hash_detects_a_tampered_plan_hash() -> None:
    plan = flow_plan(basic)
    plan.hash = "sha256:" + "0" * 64
    found = issues_of(validate(plan), "plan.hash")
    assert len(found) == 1
    assert found[0].node is None


def test_plan_without_hashes_is_not_hash_checked() -> None:
    plan = flow_plan(basic)
    plan.hash = ""
    for node, _ancestors in plan.walk():
        node.hash = ""
    plan.nodes[1].args["route"] = "/elsewhere"
    assert "plan.hash" not in codes(validate(plan))


def test_node_duplicate() -> None:
    plan = flow_plan(basic)
    assert "node.duplicate" not in codes(validate(plan))
    plan.nodes.append(plan.nodes[2].model_copy(deep=True))
    found = issues_of(validate(plan), "node.duplicate")
    assert [issue.node for issue in found] == [plan.nodes[2].id]


def test_resource_duplicate() -> None:
    plan = flow_plan(basic)
    plan.resources.append(plan.resources[0].model_copy())
    [issue] = issues_of(validate(plan), "node.duplicate")
    assert issue.node is None
    assert plan.resources[0].id in issue.message


def test_resource_kind() -> None:
    plan = flow_plan(basic)
    assert "resource.kind" not in codes(validate(plan))
    plan.resources.append(Resource(id="rzzzzzzz", kind="lease.tv"))
    [issue] = issues_of(validate(plan), "resource.kind")
    assert "lease.tv" in issue.message


def test_resource_unknown() -> None:
    plan = flow_plan(basic)
    assert "resource.unknown" not in codes(validate(plan))
    plan.nodes[2].uses.append("r0000000")
    [issue] = issues_of(validate(plan), "resource.unknown")
    assert issue.node == plan.nodes[2].id
    assert "r0000000" in issue.message


def test_op_unknown_skips_the_other_checks() -> None:
    plan = flow_plan(basic)
    assert "op.unknown" not in codes(validate(plan))
    tap = plan.nodes[2]
    tap.op = "ui.teleport"
    tap.args = {
        "nonsense": True,
        "x": {"$ref": "nzzzzzzzzzz", "path": []},
        "y": {"$ref": 5},
        "z": {"$item": "nqqqqqqqqqq", "path": []},
    }
    tap.v = "9"
    tap.uses.append("r0000000")
    tap.after.append("nyyyyyyyyyy")
    report = validate(plan)
    [issue] = issues_of(report, "op.unknown")
    assert issue.node == tap.id
    assert "ui.teleport" in issue.message
    node_codes = {found.code for found in report.issues if found.node == tap.id}
    assert node_codes <= {"op.unknown", "plan.hash"}
    assert {found.code for found in report.issues} <= {"op.unknown", "plan.hash"}


class Beam(Args):
    target: str


def test_op_unknown_of_an_unloaded_op_is_the_only_issue(eks_registry: Registry) -> None:
    # a plan built while a plugin op was registered (with an executor version) and validated after it is gone
    state = eks_registry.snapshot()
    eks_registry.register_op(OpSpec(name="acme.beam_probe", family="acme", args=Beam, executor_version="3"))

    def body(s: SessionCore) -> Any:
        s.record_op("ui.tap", {"target": by.text("Kaydet")})
        beamed = s.record_op("acme.beam_probe", {"target": "dock"})
        s.record_op("ui.nav", {"route": "/rows"})
        return {"beamed": beamed}

    plan = flow_plan(body)
    assert validate(plan, registry=eks_registry).issues == ()
    eks_registry.restore(state)
    report = validate(plan, registry=eks_registry)
    beam = plan.nodes[1]
    assert beam.op == "acme.beam_probe"
    assert [(issue.code, issue.node) for issue in report.issues] == [("op.unknown", beam.id)]


def test_op_version() -> None:
    plan = flow_plan(basic)
    assert "op.version" not in codes(validate(plan))
    plan.nodes[2].v = "2"
    [issue] = issues_of(validate(plan), "op.version")
    assert issue.node == plan.nodes[2].id


def test_args_invalid_lists_the_first_three_errors() -> None:
    plan = flow_plan(basic)
    assert "args.invalid" not in codes(validate(plan))
    nav = plan.nodes[1]
    nav.args = {"route": "/x", "one": 1, "two": 2, "three": 3, "four": 4}
    [issue] = issues_of(validate(plan), "args.invalid")
    assert issue.node == nav.id
    assert "one: Extra inputs are not permitted" in issue.message
    assert "three: " in issue.message
    assert "four" not in issue.message
    assert issue.message.endswith("and 1 more")


def test_plain_op_with_branches() -> None:
    plan = flow_plan(basic)
    assert "node.branches" not in codes(validate(plan))
    plan.nodes[2].branches = {"body": []}
    [issue] = issues_of(validate(plan), "node.branches")
    assert issue.node == plan.nodes[2].id


def test_container_branches() -> None:
    def body(s: SessionCore) -> None:
        with eks.parallel():
            s.record_op("ui.tap", {"target": by.test_id("a")})

    plan = flow_plan(body)
    assert "node.branches" not in codes(validate(plan))
    parallel = plan.nodes[0]
    assert parallel.branches is not None
    parallel.branches["other"] = []
    found = issues_of(validate(plan), "node.branches")
    assert len(found) == 1
    assert "'other'" in found[0].message
    parallel.branches = {}
    found = issues_of(validate(plan), "node.branches")
    assert len(found) == 1
    assert "'body'" in found[0].message


def test_node_after_unknown_and_later() -> None:
    plan = flow_plan(basic)
    assert "node.after" not in codes(validate(plan))
    plan.nodes[1].after.append("nzzzzzzzzzz")
    plan.nodes[0].after.append(plan.nodes[2].id)
    found = issues_of(validate(plan), "node.after")
    assert [issue.node for issue in found] == [plan.nodes[0].id, plan.nodes[1].id]
    assert "nzzzzzzzzzz" in found[1].message

    def recorded(s: SessionCore) -> None:
        with s.record_container("capture.record", {"name": "demo"}):
            s.record_op("ui.tap", {"target": by.text("Kaydet")})

    plan = flow_plan(recorded)
    assert "node.after" not in codes(validate(plan))
    [record] = [node for node, _ancestors in plan.walk() if node.op == "capture.record"]
    assert record.branches is not None
    tap = record.branches["body"][0]
    # waiting on the enclosing recording deadlocks: the container only finishes after its body
    tap.after.append(record.id)
    [issue] = issues_of(validate(plan), "node.after")
    assert issue.node == tap.id
    assert "enclosing container" in issue.message
    assert record.id in issue.message


def test_node_cycle() -> None:
    plan = flow_plan(basic)
    assert "node.cycle" not in codes(validate(plan))
    first, second = plan.nodes[0], plan.nodes[1]
    assert first.id in second.after
    first.after.append(second.id)
    [issue] = issues_of(validate(plan), "node.cycle")
    assert issue.node == first.id
    assert first.id in issue.message
    assert second.id in issue.message


def test_self_edge_is_a_cycle() -> None:
    plan = flow_plan(basic)
    plan.nodes[2].after.append(plan.nodes[2].id)
    [issue] = issues_of(validate(plan), "node.cycle")
    assert issue.node == plan.nodes[2].id


def when_otherwise(s: SessionCore) -> None:
    exists = s.record_op("ui.exists", {"target": by.test_id("empty")})
    with eks.when(exists):
        s.record_op("ui.tap", {"target": by.test_id("create")})
    with eks.otherwise():
        s.record_op("ui.tap", {"target": by.test_id("row")})


def test_control_else() -> None:
    plan = flow_plan(when_otherwise)
    report = validate(plan)
    assert report.ok
    assert "control.else" not in codes(report)
    when = plan.nodes[1]
    assert when.op == "ctl.when"
    assert when.branches is not None
    del when.branches["then"]
    report = validate(plan)
    [issue] = issues_of(report, "control.else")
    assert issue.node == when.id
    assert "node.branches" not in codes(report)


def test_control_empty_is_a_warning() -> None:
    def body(s: SessionCore) -> None:
        exists = s.record_op("ui.exists", {"target": by.test_id("empty")})
        with eks.when(exists):
            pass

    report = validate(flow_plan(body))
    assert report.ok
    [issue] = report.warnings
    assert issue.code == "control.empty"
    assert issue.severity == "warning"
    assert report.text().startswith("WARNING control.empty ")
    assert "control.empty" not in codes(validate(flow_plan(when_otherwise)))


def test_report_text_and_ordering() -> None:
    plan = flow_plan(basic)
    plan.schema_ = "eks/plan@0"  # type: ignore[assignment]
    tap = plan.nodes[2]
    tap.v = "7"
    tap.uses.append("r0000000")
    plan.nodes[0].v = "3"
    report = validate(plan)
    lines = report.text().splitlines()
    assert lines[0].startswith("ERROR plan.schema: ")
    node_codes = [(issue.node, issue.code) for issue in report.issues if issue.node is not None]
    assert node_codes == [
        (plan.nodes[0].id, "op.version"),
        (tap.id, "op.version"),
        (tap.id, "resource.unknown"),
    ]
    assert lines[1] == f"ERROR op.version {plan.nodes[0].id} {plan.nodes[0].src}: {report.issues[1].message}"
    assert len(lines) == len(report.issues)


def test_text_shows_the_field() -> None:
    plan = flow_plan(basic)
    plan.nodes[1].args["route"] = {"$ref": "nzzzzzzzzzz", "path": []}
    [issue] = issues_of(validate(plan), "ref.unknown")
    assert issue.field == "route"
    assert f"{issue.src}: route: " in issue.text()


def test_as_dict_shape() -> None:
    plan = flow_plan(basic)
    plan.nodes[2].v = "2"
    data = validate(plan).as_dict()
    assert set(data) == {"ok", "hash", "issues"}
    assert data["ok"] is False
    assert data["hash"] == plan.hash
    issue = next(item for item in data["issues"] if item["code"] == "op.version")
    assert set(issue) == {"severity", "code", "message", "node", "field", "src"}
    assert issue["severity"] == "error"
    assert issue["node"] == plan.nodes[2].id


def test_validator_class_and_determinism(eks_registry: Registry) -> None:
    plan = flow_plan(when_otherwise)
    plan.nodes[0].v = "5"
    plan.nodes[1].after.append("nzzzzzzzzzz")
    validator = Validator(eks_registry)
    first = validator.validate(plan).as_dict()
    second = validator.validate(plan).as_dict()
    assert first == second
    assert validate(plan).as_dict() == first


def test_validator_never_raises_for_broken_nodes() -> None:
    plan = flow_plan(basic)
    plan.nodes.append(Node.model_construct(id="nzzzzzzzzzz", op="ui.tap", v="1", args={"target": "not a target"},
                                           uses=[], after=["n0000000000"], out="none", pin=None, label=None,
                                           src=None, branches={"x": []}, hash=""))
    report = validate(plan)
    assert not report.ok
    assert {"args.invalid", "node.after", "node.branches"} <= set(codes(report))
