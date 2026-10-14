from __future__ import annotations

from collections.abc import Callable
from typing import Any

import eks
from eks import by
from eks.tracing import SessionCore
from eks_harness.engine.validator import Issue, Report, validate
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry
from eks_harness.ir.target import Platform


def flow_plan(body: Callable[[SessionCore], Any], *, platform: Platform = "web") -> Plan:
    # a frozen now keeps the clock.now warning out of these reports
    @eks.flow(app="web-app", platform=platform, now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul", locale="tr-TR")
    def probe(s: eks.Session) -> Any:
        return body(s)

    return eks.plan(probe)


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def ref(node_id: str, *path: Any) -> dict[str, Any]:
    return {"$ref": node_id, "path": list(path)}


def test_ref_unknown() -> None:
    def body(s: SessionCore) -> None:
        text = s.record_op("ui.text", {"target": by.test_id("title")})
        s.record_op("ui.nav", {"route": f"/x/{text}"})

    plan = flow_plan(body)
    assert validate(plan).ok
    nav = by_op(plan, "ui.nav")[0]
    nav.args["route"] = {"$fmt": ["/x/", ref("nzzzzzzzzzz")]}
    [issue] = issues_of(validate(plan), "ref.unknown")
    assert issue.node == nav.id
    assert issue.field == "route"
    assert "nzzzzzzzzzz" in issue.message


def test_ref_into_a_when_body_from_outside() -> None:
    def body(s: SessionCore) -> None:
        exists = s.record_op("ui.exists", {"target": by.test_id("empty")})
        with eks.when(exists):
            inner = s.record_op("ui.text", {"target": by.test_id("title")})
            s.record_op("ui.nav", {"route": f"/inside/{inner}"})
        s.record_op("ui.nav", {"route": f"/after/{inner}"})

    plan = flow_plan(body)
    report = validate(plan)
    found = issues_of(report, "ref.not_visible")
    after = by_op(plan, "ui.nav")[1]
    assert [issue.node for issue in found] == [after.id]
    assert found[0].field == "route"
    assert "when, each or dynamic" in found[0].message


def test_ref_into_an_each_body_from_outside() -> None:
    def body(s: SessionCore) -> None:
        rows = s.record_op("api.get", {"path": "/api/rows"})
        with eks.each(rows["items"]) as row:
            inner = s.record_op("ui.text", {"target": by.test_id(f"row-{row.id}")})
        s.record_op("ui.nav", {"route": f"/after/{inner}"})

    plan = flow_plan(body)
    found = issues_of(validate(plan), "ref.not_visible")
    assert [issue.node for issue in found] == [by_op(plan, "ui.nav")[0].id]


def test_plan_outputs_referencing_a_when_body() -> None:
    def body(s: SessionCore) -> Any:
        exists = s.record_op("ui.exists", {"target": by.test_id("empty")})
        with eks.when(exists):
            inner = s.record_op("ui.text", {"target": by.test_id("title")})
        return {"title": inner, "exists": exists}

    plan = flow_plan(body)
    [issue] = issues_of(validate(plan), "ref.not_visible")
    assert issue.node is None
    assert "plan outputs" in issue.message


def test_refs_into_macro_and_record_bodies_are_visible(eks_registry: Registry) -> None:
    @eks.macro(namespace="shop", name="login")
    def login(s: Any) -> None:
        del s

    def body(s: SessionCore) -> Any:
        with s.record_container("ctl.macro", {"name": "shop.login", "version": "1", "params": {}}):
            from_macro = s.record_op("ui.text", {"target": by.test_id("user")})
        with s.record_container("capture.record", {"name": "demo"}):
            from_record = s.record_op("ui.text", {"target": by.test_id("total")})
        with eks.parallel():
            from_parallel = s.record_op("ui.text", {"target": by.test_id("side")})
        s.record_op("ui.nav", {"route": f"/u/{from_macro}/{from_record}/{from_parallel}"})
        return {"user": from_macro, "total": from_record}

    report = validate(flow_plan(body))
    assert report.ok, report.text()
    assert report.issues == ()


def test_refs_to_later_or_enclosing_nodes() -> None:
    def body(s: SessionCore) -> None:
        exists = s.record_op("ui.exists", {"target": by.test_id("empty")})
        with eks.when(exists):
            s.record_op("ui.nav", {"route": "/inside"})
        s.record_op("ui.text", {"target": by.test_id("late")})

    plan = flow_plan(body)
    when = by_op(plan, "ctl.when")[0]
    inside = by_op(plan, "ui.nav")[0]
    late = by_op(plan, "ui.text")[0]
    inside.args["route"] = {"$fmt": ["/x/", ref(when.id), "/", ref(late.id)]}
    found = issues_of(validate(plan), "ref.not_visible")
    assert [issue.node for issue in found] == [inside.id, inside.id]
    assert when.id in found[0].message
    assert late.id in found[1].message


def test_item_scope() -> None:
    def body(s: SessionCore) -> None:
        rows = s.record_op("api.get", {"path": "/api/rows"})
        with eks.each(rows["items"]) as row:
            s.record_op("ui.nav", {"route": f"/rows/{row.id}"})
        s.record_op("ui.nav", {"route": "/done"})

    plan = flow_plan(body)
    assert validate(plan).ok
    each = by_op(plan, "ctl.each")[0]
    done = by_op(plan, "ui.nav")[1]
    done.args["route"] = {"$fmt": ["/rows/", {"$item": each.id, "path": ["id"]}]}
    [issue] = issues_of(validate(plan), "ref.item_scope")
    assert issue.node == done.id
    assert each.id in issue.message


def test_item_types_follow_the_list_type() -> None:
    def body(s: SessionCore) -> None:
        names = s.record_op("ui.state", {"path": "names"})
        count = s.record_op("ui.count", {"target": by.test_id("row")})
        with eks.each(names) as name:
            s.record_op("ui.nav", {"route": f"/n/{name}"})
        s.record_op("ui.nav", {"route": f"/c/{count}"})

    plan = flow_plan(body)
    assert validate(plan).ok
    each = by_op(plan, "ctl.each")[0]
    count = by_op(plan, "ui.count")[0]
    inner = by_op(plan, "ui.nav")[0]
    # each over a json value gives json items, which fit anything
    inner.args["route"] = {"$item": each.id, "path": []}
    assert issues_of(validate(plan), "ref.type") == []
    # an each over an int is not a list: its items are json, and a key on them is fine
    each.args["over"] = ref(count.id)
    inner.args["route"] = {"$item": each.id, "path": ["x"]}
    assert issues_of(validate(plan), "ref.path") == []


def test_ref_path_on_an_int_producer() -> None:
    def body(s: SessionCore) -> None:
        count = s.record_op("ui.count", {"target": by.test_id("row")})
        s.record_op("ui.nav", {"route": f"/rows/{count}"})

    plan = flow_plan(body)
    assert validate(plan).ok
    count = by_op(plan, "ui.count")[0]
    nav = by_op(plan, "ui.nav")[0]
    nav.args["route"] = {"$fmt": ["/rows/", ref(count.id, "total")]}
    report = validate(plan)
    [issue] = issues_of(report, "ref.path")
    assert issue.node == nav.id
    assert issue.field == "route"
    assert "key 'total'" in issue.message
    assert issues_of(report, "ref.type") == []


def test_ref_path_inside_find_values_and_outputs() -> None:
    def body(s: SessionCore) -> Any:
        branches = s.record_op("api.get", {"path": "/api/branches"})
        name = s.record_op("ui.text", {"target": by.test_id("name")})
        kadikoy = branches["items"].find(name=name).id
        s.record_op("ui.nav", {"route": f"/b/{kadikoy}"})
        return {"branches": branches}

    plan = flow_plan(body)
    assert validate(plan).ok
    text = by_op(plan, "ui.text")[0]
    plan.outputs = {"bad": ref(text.id, 0)}
    [issue] = issues_of(validate(plan), "ref.path")
    assert issue.node is None


def test_ref_type_int_into_a_text_field() -> None:
    def body(s: SessionCore) -> None:
        count = s.record_op("ui.count", {"target": by.test_id("row")})
        s.record_op("ui.nav", {"route": f"/rows/{count}"})

    plan = flow_plan(body)
    assert issues_of(validate(plan), "ref.type") == []
    count = by_op(plan, "ui.count")[0]
    nav = by_op(plan, "ui.nav")[0]
    nav.args["route"] = ref(count.id)
    [issue] = issues_of(validate(plan), "ref.type")
    assert issue.node == nav.id
    assert issue.field == "route"
    assert "produces int but the field takes str" in issue.message
    assert "f-string" in issue.message


def test_ref_type_inside_fmt_rejects_artifacts() -> None:
    def body(s: SessionCore) -> None:
        shot = s.record_op("capture.shot", {"name": "liste"})
        s.record_op("ui.nav", {"route": f"/a/{shot.id}"})

    plan = flow_plan(body)
    assert validate(plan).ok
    shot = by_op(plan, "capture.shot")[0]
    nav = by_op(plan, "ui.nav")[0]
    nav.args["route"] = {"$fmt": ["/a/", ref(shot.id)]}
    [issue] = issues_of(validate(plan), "ref.type")
    assert "artifact" in issue.message


def test_ref_type_media_fields() -> None:
    def body(s: SessionCore) -> None:
        clip = s.record_op("api.get", {"path": "/api/media"})
        flag = s.record_op("ui.exists", {"target": by.test_id("camera")})
        s.record_op("fake.arm", {"slot": "camera", "media": clip})
        s.record_op("fake.arm", {"slot": "gallery", "media": flag})

    plan = flow_plan(body, platform="ios")
    report = validate(plan)
    arms = by_op(plan, "fake.arm")
    flag = by_op(plan, "ui.exists")[0]
    found = issues_of(report, "ref.type")
    assert [issue.node for issue in found] == [arms[1].id]
    assert found[0].field == "media"
    assert "produces bool but the field takes media" in found[0].message
    assert flag.out == "bool"


def test_ref_in_a_target_argument() -> None:
    def body(s: SessionCore) -> None:
        name = s.record_op("ui.text", {"target": by.test_id("name")})
        s.record_op("ui.tap", {"target": by.text(name)})

    plan = flow_plan(body)
    assert validate(plan).ok
    name = by_op(plan, "ui.text")[0]
    tap = by_op(plan, "ui.tap")[0]
    tap.args["target"] = {"by": "text", "args": {"text": {"$fmt": [ref(name.id, "first"), "x"]}}}
    assert issues_of(validate(plan), "ref.path")
    tap.args["target"] = {"by": "text", "args": {"text": ref("nzzzzzzzzzz")}}
    [issue] = issues_of(validate(plan), "ref.unknown")
    assert issue.field == "target.args.text"


def test_ref_type_in_check_params_is_reported_once() -> None:
    # check params are scanned once, with the check's own model, not again as raw JSON
    def body(s: SessionCore) -> None:
        shot = s.record_op("capture.shot", {"name": "liste"})
        s.record_op("check.checkpoint", {"label": "liste",
                                         "checks": [{"kind": "route", "params": {"expected": f"/a/{shot.id}"}}]})

    plan = flow_plan(body)
    assert validate(plan).ok, validate(plan).text()
    shot = by_op(plan, "capture.shot")[0]
    checkpoint = by_op(plan, "check.checkpoint")[0]
    checkpoint.args["checks"] = [{"kind": "route", "params": {"expected": {"$fmt": ["/a/", ref(shot.id)]}}}]
    report = validate(plan)
    [issue] = issues_of(report, "ref.type")
    assert issue.node == checkpoint.id
    assert issue.field == "checks.0.params.expected"
    assert "artifact" in issue.message
    assert report.text().count("ref.type") == 1


def test_ref_type_in_media_check_params_is_reported_once() -> None:
    def body(s: SessionCore) -> None:
        clip = s.record_op("api.get", {"path": "/api/media"})
        shot = s.record_op("capture.shot", {"name": "liste"})
        s.record_op("check.media", {"media": clip, "kind": "text",
                                    "params": {"text": "Merhaba", "region": by.text(f"{shot.id}")}})

    plan = flow_plan(body)
    assert validate(plan).ok, validate(plan).text()
    shot = by_op(plan, "capture.shot")[0]
    check = by_op(plan, "check.media")[0]
    check.args["params"] = {"text": "Merhaba",
                            "region": {"by": "text", "args": {"text": {"$fmt": ["/a/", ref(shot.id)]}}}}
    report = validate(plan)
    [issue] = issues_of(report, "ref.type")
    assert issue.node == check.id
    assert issue.field == "params.region.args.text"
    assert "artifact" in issue.message


def test_malformed_expressions_in_json_args() -> None:
    def body(s: SessionCore) -> Any:
        title = s.record_op("ui.text", {"target": by.test_id("title")})
        s.record_op("api.post", {"path": "/api/notes", "json": {"title": title, "tags": ["a"]}})
        return {"title": title}

    plan = flow_plan(body)
    assert validate(plan).ok, validate(plan).text()
    post = by_op(plan, "api.post")[0]
    title = by_op(plan, "ui.text")[0]
    # RJson fields accept any dict, so only the validator catches hand-edited expression objects
    post.args["json"] = {
        "title": {"$ref": 5, "path": []},
        "tags": [{"$ref": "nzzzzzzzzzz", "path": [{"weird": 1}]}],
        "items": {"$item": [], "path": []},
        "ok": ref(title.id),
    }
    report = validate(plan)
    found = issues_of(report, "ref.path")
    assert [issue.field for issue in found] == ["json.title", "json.tags.0", "json.items"]
    assert all(issue.node == post.id for issue in found)
    assert all("malformed ref expression" in issue.message for issue in found)
    assert issues_of(report, "ref.unknown") == []
    assert issues_of(report, "args.invalid") == []
    assert not report.ok


def test_malformed_expression_in_a_typed_field_is_reported_once() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("api.post", {"path": "/api/notes"})

    plan = flow_plan(body)
    post = by_op(plan, "api.post")[0]
    post.args["path"] = {"$ref": 5, "path": []}
    report = validate(plan)
    assert [issue.code for issue in report.issues] == ["args.invalid"]


def test_malformed_expression_in_plan_outputs() -> None:
    def body(s: SessionCore) -> Any:
        return {"title": s.record_op("ui.text", {"target": by.test_id("title")})}

    plan = flow_plan(body)
    plan.outputs = {"title": {"$ref": "nzzzzzzzzzz", "path": [{"weird": 1}]}}
    [issue] = validate(plan).issues
    assert issue.code == "ref.path"
    assert issue.node is None
    assert issue.field == "title"
    assert "plan outputs" in issue.message
