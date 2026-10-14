from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import eks
from eks import by, checks, expect, t
from eks.tracing import TraceError, current
from eks_harness.engine.validator import validate
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry


def flow_entry(name: str) -> Any:
    return eks.flow(name=name, app="web-app", platform="web", now="2026-01-15T09:00+03:00",
                    timezone="Europe/Istanbul", locale="tr-TR")


def stripped(plan: Plan) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(plan.to_json())
    data.pop("hash", None)

    def clear(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            node.pop("hash", None)
            for children in (node.get("branches") or {}).values():
                clear(children)

    clear(data["nodes"])
    return data


def issues(data: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(issue.code, issue.field) for issue in validate(Plan.model_validate(data)).issues]


def find(data: dict[str, Any], op: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if node["op"] == op:
                found.append(node)
            for children in (node.get("branches") or {}).values():
                walk(children)

    walk(data["nodes"])
    return found


# Node.out is checked against the op


def counted() -> Plan:
    @flow_entry("counted")
    def flow(s: eks.Session) -> Any:
        n = s.ui.count("#row")
        items = s.api.get("/api/items")
        s.ui.fill("#x", f"{n}")
        with eks.each(items):
            s.ui.tap(by.test_id("row"))

    return eks.plan(flow)


def test_a_forged_out_is_reported_and_ignored_for_typing() -> None:
    data = stripped(counted())
    count = find(data, "ui.count")[0]
    find(data, "ui.fill")[0]["args"]["value"] = {"$ref": count["id"], "path": []}
    find(data, "ctl.each")[0]["args"]["over"] = {"$ref": count["id"], "path": []}
    honest = issues(data)
    assert honest == [("ref.type", "value"), ("ref.type", "over")]
    count["out"] = "json"
    assert issues(data) == [("op.out", "out"), *honest]


def test_an_omitted_out_names_the_missing_field() -> None:
    data = stripped(counted())
    count = find(data, "ui.count")[0]
    count.pop("out")
    report = validate(Plan.model_validate(data))
    assert [issue.code for issue in report.issues] == ["op.out"]
    assert "no out field" in report.issues[0].message


def test_a_data_create_out_names_its_resource_kind() -> None:
    @eks.resource("federation")
    class Federation(eks.Resource):
        type: str

    @eks.seed("outs", backend="eksenapi", now="2026-01-15T09:00+03:00")
    def outs(d: Any) -> None:
        d.federation("anadolu-bayi", type="Reseller")

    data = stripped(eks.plan(outs))
    create = find(data, "data.create")[0]
    assert create["out"] == "resource:federation"
    create["out"] = "resource:tenant"
    assert ("op.out", "out") in issues(data)


def test_the_tracer_refuses_an_out_that_widens_the_op(eks_registry: Registry) -> None:
    @flow_entry("widened")
    def widened(s: eks.Session) -> None:
        current().record("ui.count", {"target": {"by": "css", "args": {"selector": "#row"}}},
                         uses=[s.resources["lease"]], out="json")

    with pytest.raises(TraceError, match="ui.count produces int, not json"):
        eks.plan(widened)


# after edges cover refs, clock providers and lanes, and respect visibility


def edges() -> Plan:
    @flow_entry("edges")
    def flow(s: eks.Session) -> None:
        branches = s.api.get("/api/tenant-branches")
        s.ui.nav(f"/tenant/invoice/list?branchId={branches.items[0].id}")
        s.ui.tap(by.test_id("save"))
        with eks.when(s.ui.exists("#empty")):
            s.ui.tap(by.test_id("create"))
        s.ui.tap(by.test_id("after-when"))

    return eks.plan(flow)


def test_the_traced_plan_waits_for_everything_it_reads() -> None:
    assert issues(stripped(edges())) == []


def test_a_consumer_without_an_edge_to_its_producer_is_reported() -> None:
    data = stripped(edges())
    find(data, "ui.nav")[0]["after"] = []
    assert ("node.after", "after") in issues(data)


def test_a_program_node_must_follow_its_lane_tail() -> None:
    data = stripped(edges())
    taps = [node for node in data["nodes"] if node["op"] == "ui.tap"]
    taps[-1]["after"] = []
    report = validate(Plan.model_validate(data))
    assert [(issue.code, issue.node) for issue in report.issues] == [("node.after", taps[-1]["id"])]
    assert "does not wait for it" in report.issues[0].message


def test_an_edge_into_a_when_branch_is_not_visible() -> None:
    data = stripped(edges())
    when = find(data, "ctl.when")[0]
    last = data["nodes"][-1]
    last["after"] = [when["id"], when["branches"]["then"][0]["id"]]
    report = validate(Plan.model_validate(data))
    assert [issue.code for issue in report.issues] == ["node.after"]
    assert "may never run" in report.issues[0].message


def test_a_graph_ordered_render_must_wait_for_the_sources_it_reads() -> None:
    @eks.edit(name="cut", fps=30, size=(1080, 1920), duration=t("4s"))
    def cut(e: eks.Edit) -> None:
        e.track("main").clip(e.media("artifact:VID123"), start=t("0s"), src=t.span(t("0s"), t("4s")))

    data = stripped(eks.plan(cut))
    render = find(data, "edit.render")[0]
    assert render["after"]
    render["after"] = []
    assert issues(data) == [("node.after", "after")]


def test_a_hand_written_clock_edge_must_point_backwards(eks_registry: Registry) -> None:
    @eks.scene(name="pulse_card", kind="gfx", size=(1080, 1920), duration=t("8s"), fps=30)
    def card(sc: eks.GfxScene) -> None:
        title = sc.text("EksenGOLD", id="title")
        sc.on(t.beats("downbeat")[0:8], title.pulse(amount=1.06))

    @eks.edit(name="pulsed", fps=30, size=(1080, 1920), duration=t("8s"))
    def pulsed(e: eks.Edit) -> None:
        e.audio.track("artifact:SONG1", name="song").beats("beat", "downbeat")
        e.track("graphics").clip(e.gfx(card))

    data = stripped(eks.plan(pulsed))
    render = find(data, "gfx.render")[0]
    markers = find(data, "media.markers")[0]
    render["after"] = [edge for edge in render["after"] if edge != markers["id"]]
    assert issues(data) == [("node.after", "after")]


# expect.eventually only polls idempotent reads


def polled() -> Plan:
    @flow_entry("polled")
    def flow(s: eks.Session) -> None:
        status = s.api.get("/api/jobs/1")
        expect.eventually(status.state, timeout=t("10s")).eq("done")
        created = s.api.post("/api/invoices", json={"amount": 5})
        expect(created.id).truthy()
        with eks.each(s.api.get("/api/rows").items) as row:
            expect(row.id).truthy()

    return eks.plan(flow)


def test_polling_an_idempotent_read_is_clean() -> None:
    assert issues(stripped(polled())) == []


def test_polling_a_mutating_request_is_reported() -> None:
    data = stripped(polled())
    second = find(data, "expect.value")[1]
    second["args"]["eventually"] = True
    second["args"]["timeout"] = "10s"
    assert issues(data) == [("expect.eventually", "eventually")]


def test_polling_an_each_item_is_reported() -> None:
    data = stripped(polled())
    looped = find(data, "expect.value")[2]
    looped["args"]["eventually"] = True
    assert issues(data) == [("expect.eventually", "eventually")]


def test_polling_a_literal_is_reported() -> None:
    data = stripped(polled())
    first = find(data, "expect.value")[0]
    first["args"]["value"] = "done"
    assert ("expect.eventually", "eventually") in issues(data)


# literal evidence names inside loops repeat


def test_literal_evidence_names_inside_a_runtime_each_are_duplicates() -> None:
    @flow_entry("looped")
    def looped(s: eks.Session) -> None:
        invoices = s.api.get("/api/invoices")
        with eks.each(invoices.items) as invoice:
            s.ui.nav(f"/tenant/invoice/{invoice.id}")
            s.checkpoint("fatura", checks=[checks.use("visual")])
            with s.net.cassette("fatura-api", mode="replay"):
                s.ui.reload()
            s.shot("liste")

    report = validate(eks.plan(looped))
    found = [(issue.severity, issue.code, issue.field) for issue in report.issues]
    assert found == [
        ("error", "evidence.duplicate", "label"),
        ("error", "evidence.duplicate", "name"),
        ("warning", "evidence.duplicate", "name"),
    ]


def test_evidence_names_built_from_the_loop_item_are_clean() -> None:
    @flow_entry("named")
    def named(s: eks.Session) -> None:
        invoices = s.api.get("/api/invoices")
        with eks.each(invoices.items) as invoice:
            s.ui.nav(f"/tenant/invoice/{invoice.id}")
            s.checkpoint(f"fatura-{invoice.number}", checks=[checks.use("visual")])

    assert validate(eks.plan(named)).issues == ()


def test_literal_evidence_names_in_a_dynamic_loop_are_duplicates() -> None:
    @flow_entry("dynamic_shots")
    def dynamic_shots(s: eks.Session) -> None:
        @eks.dynamic
        def per_row(n=s.ui.count("#row")) -> None:  # noqa: B008
            for index in range(n):
                s.ui.tap(by.test_id(f"row-{index}"))
                s.shot("row")

    report = validate(eks.plan(dynamic_shots))
    assert [(issue.severity, issue.code) for issue in report.issues] == [("warning", "evidence.duplicate")]


def test_the_traced_plans_keep_validating_after_a_deep_copy() -> None:
    data = stripped(edges())
    assert issues(copy.deepcopy(data)) == []
