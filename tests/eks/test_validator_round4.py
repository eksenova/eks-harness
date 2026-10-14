from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import eks
from eks import t
from eks.checks import use
from eks.planning import PlanBuildError
from eks.tracing import TraceError
from eks.workspace import plan_spec
from eks_harness.engine.validator import ISSUE_CODES, Issue, Report, validate
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry
from eks_harness.ir.target import Platform

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = ROOT / "examples" / "dsl"
NOW = "2026-01-15T09:00+03:00"
ZONE = {"timezone": "Europe/Istanbul", "locale": "tr-TR"}


def flow_plan(body: Callable[[eks.Session], Any], *, platform: Platform = "web", **options: Any) -> Plan:
    options.setdefault("now", NOW)
    for name, value in ZONE.items():
        options.setdefault(name, value)
    app = "web-app" if platform == "web" else "mobile-app"

    @eks.flow(app=app, platform=platform, **options)
    def probe(s: eks.Session) -> Any:
        return body(s)

    return eks.plan(probe)


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "dsl"
    shutil.copytree(EXAMPLES_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def write_flow(tree: Path, name: str, source: str) -> str:
    path = tree / ".harness" / "flows" / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path.relative_to(tree).as_posix()


def test_new_codes_are_documented() -> None:
    for code in ("time.reversed", "resource.backend", "cassette.nested", "evidence.duplicate", "clock.zone"):
        assert code in ISSUE_CODES


# ctl.each over a ref that is not a list


def test_each_over_a_scalar_ref_is_a_ref_type_issue() -> None:
    # a hand-written plan (the tracer refuses this) whose loop iterates over a count
    def body(s: eks.Session) -> None:
        rows = s.api.get("/api/rows")
        with eks.each(rows.items) as row:
            s.ui.nav(f"/rows/{row.id}")

    plan = flow_plan(body)
    assert validate(plan).issues == ()
    count = by_op(plan, "api.get")[0]
    count.out = "int"
    (loop,) = by_op(plan, "ctl.each")
    loop.args["over"] = {"$ref": count.id, "path": []}
    [issue] = issues_of(validate(plan), "ref.type")
    assert issue.node == loop.id
    assert issue.field == "over"
    assert "list[json]" in issue.message


@pytest.mark.parametrize("out", ["str", "bool", "artifact", "media"])
def test_each_over_other_scalars_is_reported(out: str) -> None:
    def body(s: eks.Session) -> None:
        rows = s.api.get("/api/rows")
        with eks.each(rows):
            s.ui.back()

    plan = flow_plan(body)
    by_op(plan, "api.get")[0].out = out
    assert [issue.field for issue in issues_of(validate(plan), "ref.type")] == ["over"]


def test_each_over_lists_and_json_stays_valid() -> None:
    def body(s: eks.Session) -> None:
        names = s.ctx.record("api.get", {"path": "/api/names"}, uses=[s.ctx.session_backend(s.spec)],
                             out="list[str]")
        with eks.each(names) as name:
            s.ui.nav(f"/n/{name}")
        with eks.each(s.api.get("/api/rows")) as row:
            s.ui.nav(f"/r/{row.id}")

    assert validate(flow_plan(body)).issues == ()


# expect(ref) assertions against the producer type


@pytest.mark.parametrize(("read", "check", "message"), [
    (lambda s: s.ui.count("#x"), lambda e: e.contains("a"), "needs a str, list or json value"),
    (lambda s: s.ui.count("#x"), lambda e: e.matches("^a"), "needs a str or json value"),
    (lambda s: s.ui.exists("#x"), lambda e: e.length(at_least=1), "needs a str, list or json value"),
    (lambda s: s.ui.text("#x"), lambda e: e.gt(5), "needs an int, float or json value"),
    (lambda s: s.ui.count("#x"), lambda e: e.le("ten"), "is not a number"),
    (lambda s: s.ui.text("#x"), lambda e: e.contains(5), "can only hold text"),
])
def test_impossible_value_assertions_raise_at_trace_time(
    read: Callable[[eks.Session], Any], check: Callable[[Any], Any], message: str,
) -> None:
    def body(s: eks.Session) -> None:
        check(eks.expect(read(s)))

    with pytest.raises(TypeError, match=message):
        flow_plan(body)


def test_possible_value_assertions_trace_and_validate() -> None:
    def body(s: eks.Session) -> None:
        count = s.ui.count("#row")
        text = s.ui.text("#total")
        rows = s.api.get("/api/rows")
        eks.expect(count).gt(1)
        eks.expect(count).le(2.5)
        eks.expect(count).ge(rows.total)
        eks.expect(text).contains("TL")
        eks.expect(text).matches(r"^\d")
        eks.expect(text).length(at_least=1)
        eks.expect(text).contains(rows.currency)
        eks.expect(rows.items).contains({"id": 1})
        eks.expect(rows.items).length(at_most=50)
        eks.expect(rows.total).gt("anything goes for json")
        eks.expect(s.ui.exists("#x")).truthy()
        eks.expect(count).falsy()

    assert validate(flow_plan(body)).issues == ()


def test_the_validator_checks_assertions_against_producer_types() -> None:
    def body(s: eks.Session) -> None:
        rows = s.api.get("/api/rows")
        eks.expect(rows.total).contains("a")
        eks.expect(rows.name).gt(5)

    plan = flow_plan(body)
    assert validate(plan).issues == ()
    get = by_op(plan, "api.get")[0]
    get.out = "int"
    contains, order = by_op(plan, "expect.value")
    contains.args["value"] = {"$ref": get.id, "path": []}
    order.args["value"] = {"$ref": get.id, "path": []}
    order.args["assertion"] = {"kind": "gt", "value": "five"}
    found = issues_of(validate(plan), "ref.type")
    assert [(issue.node, issue.field) for issue in found] == [(contains.id, "assertion"), (order.id, "assertion")]
    assert "needs a str, list or json value" in found[0].message
    assert "not a number" in found[1].message


# reversed spans


def test_concretely_reversed_spans_never_reach_a_plan() -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def concrete(e: eks.Edit) -> None:
        e.track("main").clip(e.media("artifact:VID123"), src=t.span(t("10s"), t("2s")))

    with pytest.raises(ValueError, match="ends before it starts"):
        eks.plan(concrete)


def test_reversed_window_on_a_score_clock() -> None:
    @eks.score(fps=30, bpm=120, window=t.span(t("bar:4"), t("beat:8")))
    def ad(sc: eks.Score) -> None:
        sc.audio.master(loudness=-14)

    plan = eks.plan(ad)
    [issue] = issues_of(validate(plan), "time.reversed")
    assert "clock window" in issue.message
    assert "ends before it starts" in issue.message


def test_reversed_clip_source_with_a_static_tempo() -> None:
    @eks.score(fps=30, bpm=120)
    def ad(sc: eks.Score) -> None:
        sc.edit.track("main").clip(sc.edit.media("artifact:VID1"), src=t.span(t("bar:3"), t("beat:4")))

    plan = eks.plan(ad)
    [issue] = issues_of(validate(plan), "time.reversed")
    assert issue.field is not None
    assert issue.field.endswith("src")


# eks.Persona flow parameters


PERSONA_FLOW = """
import eks


@eks.flow(app="web-app", seed="standard")
def login_as(s: eks.Session, who: eks.Persona = eks.Persona("harman-admin"), note: str = "x"):
    s.ui.fill("#user", who)
    s.ui.fill("#note", note)
"""


def test_persona_parameters_are_checked_against_the_seed(tree: Path) -> None:
    spec = write_flow(tree, "login_as", PERSONA_FLOW)
    plan = plan_spec(spec, cwd=tree, plugins=True)
    assert eks.validate(plan, tree=tree).issues == ()
    plan = plan_spec(spec, params={"who": "host"}, cwd=tree, plugins=True)
    assert plan.params["who"] == "host"
    with pytest.raises(PlanBuildError, match=r"persona 'nobody' is not an output of seed 'standard'.*'harman-admin'"):
        plan_spec(spec, params={"who": "nobody"}, cwd=tree, plugins=True)
    ghost = write_flow(tree, "ghost", PERSONA_FLOW.replace('eks.Persona("harman-admin")', 'eks.Persona("ghost")')
                       .replace("def login_as", "def ghost"))
    with pytest.raises(PlanBuildError, match=r"parameter 'who': persona 'ghost'"):
        plan_spec(ghost, cwd=tree, plugins=True)


def test_optional_persona_parameters_and_flows_without_a_seed(tree: Path) -> None:
    optional = write_flow(tree, "maybe", PERSONA_FLOW.replace(
        'who: eks.Persona = eks.Persona("harman-admin")', "who: eks.Persona | None = None").replace(
        's.ui.fill("#user", who)', 'pass').replace("def login_as", "def maybe"))
    assert plan_spec(optional, cwd=tree, plugins=True).params["who"] is None
    with pytest.raises(PlanBuildError, match="persona 'nobody'"):
        plan_spec(optional, params={"who": "nobody"}, cwd=tree, plugins=True)
    seedless = write_flow(tree, "seedless", PERSONA_FLOW.replace(', seed="standard"', ', now="2026-01-15T09:00+03:00"')
                          .replace("def login_as", "def seedless"))
    assert plan_spec(seedless, params={"who": "anyone"}, cwd=tree, plugins=True).params["who"] == "anyone"


# backend definitions


def test_backend_definitions_must_be_registered(tree: Path) -> None:
    flow = write_flow(tree, "elsewhere", (
        "import eks\n\n\n@eks.flow(app='web-app', backend='no-such-backend', now='2026-01-15T09:00+03:00')\n"
        "def elsewhere(s: eks.Session):\n    s.api.get('/api/ping')\n"))
    plan = plan_spec(flow, cwd=tree, plugins=True)
    [issue] = issues_of(eks.validate(plan, tree=tree), "resource.backend")
    assert "'no-such-backend'" in issue.message
    assert "'eksenapi'" in issue.message and "'staging-readonly'" in issue.message
    assert issues_of(eks.validate(plan), "resource.backend") == []
    seeds = tree / ".harness" / "seeds" / "nope.py"
    seeds.write_text("import eks\n\n\n@eks.seed('s1', backend='nope-backend', now='2026-01-15T09:00+03:00')\n"
                     "def s1(d: eks.Data):\n    d.tenant('t1', name='T')\n", encoding="utf-8")
    seed = plan_spec(".harness/seeds/nope.py", cwd=tree, plugins=True)
    assert [issue.code for issue in eks.validate(seed, tree=tree).errors] == ["resource.backend"]


def test_legacy_contributed_backends_count(tree: Path) -> None:
    plugin = tree / ".harness" / "plugins" / "legacy"
    plugin.mkdir()
    (plugin / "harness-plugin.toml").write_text(
        '[plugin]\nid = "legacy"\nname = "Legacy"\nversion = "0.1.0"\napi = "1"\n\n'
        '[[contributes.backend]]\nid = "old-api"\nentry = "legacy_backend:OldApi"\n', encoding="utf-8")
    project = tree / ".harness" / "project.toml"
    project.write_text(project.read_text(encoding="utf-8").replace('trust = ["cookbook"]',
                                                                   'trust = ["cookbook", "legacy"]'), encoding="utf-8")
    flow = write_flow(tree, "legacy", (
        "import eks\n\n\n@eks.flow(app='web-app', backend='old-api', now='2026-01-15T09:00+03:00',"
        " timezone='UTC', locale='en-US')\ndef legacy(s: eks.Session):\n    s.api.get('/api/ping')\n"))
    plan = plan_spec(flow, cwd=tree, plugins=True)
    assert eks.validate(plan, tree=tree).issues == ()


# nested cassettes and duplicate evidence


def test_nested_cassettes_raise_and_are_reported() -> None:
    def nested(s: eks.Session) -> None:
        with s.net.cassette("a", mode="record"), s.net.cassette("b", mode="replay"):
            s.ui.nav("/x")

    with pytest.raises(TraceError, match="cannot be nested inside another cassette"):
        flow_plan(nested)

    def sequential(s: eks.Session) -> None:
        with s.net.cassette("a", mode="record"):
            s.ui.nav("/x")
        with s.net.cassette("b", mode="replay"):
            s.ui.nav("/y")

    plan = flow_plan(sequential)
    assert validate(plan).issues == ()
    first, second = by_op(plan, "net.cassette")
    assert first.branches is not None
    first.branches["body"].append(second)
    plan.nodes.remove(second)
    [issue] = issues_of(validate(plan), "cassette.nested")
    assert issue.node == second.id


def test_duplicate_evidence_names() -> None:
    def body(s: eks.Session) -> None:
        s.checkpoint("a")
        s.checkpoint("a")
        s.checkpoint("a", viewport="phone")
        s.shot("s")
        s.shot("s")
        with s.record("v1"):
            s.ui.nav("/x")
        with s.record("v1"):
            s.ui.nav("/y")
        with s.net.cassette("c", mode="record"):
            s.ui.nav("/z")
        with s.net.cassette("c", mode="replay"):
            s.ui.nav("/z")
        s.debug.break_("stop")
        s.debug.break_("stop")
        s.har()
        s.har()
        s.dump("dom")
        s.dump("dom")
        s.dump("a11y")

    found = issues_of(validate(flow_plan(body)), "evidence.duplicate")
    shown = [(issue.message.split(" is also")[0], issue.severity, issue.field) for issue in found]
    assert shown == [
        ("checkpoint 'a'", "warning", "label"),
        ("shot 's'", "warning", "name"),
        ("recording 'v1'", "warning", "name"),
        ("cassette 'c'", "error", "name"),
        ("breakpoint 'stop'", "warning", "label"),
        ("HAR 'har'", "warning", "name"),
        ("dump 'dom'", "warning", "name"),
    ]


def test_duplicate_visual_checkpoints_are_errors_and_branches_are_exclusive() -> None:
    def body(s: eks.Session) -> None:
        with eks.when(s.ui.exists("#empty")):
            s.checkpoint("liste", checks=["visual"])
        with eks.otherwise():
            s.checkpoint("liste", checks=["visual"])
        s.checkpoint(f"fatura-{s.ui.text('#no')}")
        s.checkpoint(f"fatura-{s.ui.text('#no')}")
        s.checkpoint("son", checks=[use("visual", threshold=0.01)])
        s.checkpoint("son")

    report = validate(flow_plan(body))
    [issue] = issues_of(report, "evidence.duplicate")
    assert issue.severity == "error"
    assert "visual golden" in issue.message


def test_session_visual_checks_make_duplicate_checkpoints_errors() -> None:
    def body(s: eks.Session) -> None:
        s.checkpoint("a")
        s.checkpoint("a")

    plan = flow_plan(body, checks=["visual"])
    [issue] = issues_of(validate(plan), "evidence.duplicate")
    assert issue.severity == "error"


# performance budgets per platform


def test_perf_budgets_are_split_per_platform() -> None:
    def web_on_ios(s: eks.Session) -> None:
        s.checkpoint("a", checks=[use("perf", lcp=t("2s"), cls=0.1)])

    plan = flow_plan(web_on_ios, platform="ios")
    [issue] = issues_of(validate(plan), "check.platform")
    assert "runs on web, not on ios" in issue.message

    def rn_on_web(s: eks.Session) -> None:
        s.checkpoint("a", checks=[use("perf_rn", frame_drops=3)])

    plan = flow_plan(rn_on_web)
    assert [issue.code for issue in validate(plan).errors] == ["check.platform"]
    with pytest.raises(ValueError, match="frame_drops"):
        use("perf", frame_drops=3)

    def native(s: eks.Session) -> None:
        s.checkpoint("a", checks=[use("perf_rn", frame_drops=3, requests=40)])

    assert validate(flow_plan(native, platform="android")).issues == ()


# timezone and locale


def test_frozen_flows_without_timezone_or_locale_warn() -> None:
    def body(s: eks.Session) -> None:
        s.ui.nav("/x")

    plan = flow_plan(body, timezone=None, locale=None)
    [issue] = issues_of(validate(plan), "clock.zone")
    assert issue.severity == "warning"
    assert "timezone or locale" in issue.message
    plan = flow_plan(body, locale=None)
    [issue] = issues_of(validate(plan), "clock.zone")
    assert "sets no locale" in issue.message
    assert issues_of(validate(flow_plan(body, now=None, timezone=None, locale=None)), "clock.zone") == []
    assert issues_of(validate(flow_plan(body)), "clock.zone") == []


def test_example_profiles_set_timezone_and_locale(tree: Path, eks_registry: Registry) -> None:
    plan = plan_spec(".harness/flows/subeler.py", cwd=tree, plugins=True)
    assert plan.session is not None
    assert (plan.session.timezone, plan.session.locale) == ("Europe/Istanbul", "tr-TR")
