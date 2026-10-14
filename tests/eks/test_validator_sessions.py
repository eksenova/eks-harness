from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import eks
from eks import by, t
from eks.planning import trace
from eks.tracing import SessionCore, TraceContext
from eks_harness.engine.validator import Issue, Report, validate
from eks_harness.ir.plan import Node, Plan, Requirement, SessionSpec
from eks_harness.ir.presets import PersonaPreset
from eks_harness.ir.registry import Registry
from eks_harness.ir.target import Platform
from eks_harness.ir.time import Time


def flow_plan(body: Callable[[SessionCore], Any], *, platform: Platform = "web", **options: Any) -> Plan:
    # a frozen now keeps the clock.now warning out of these reports unless a test asks for its absence
    options.setdefault("now", "2026-01-15T09:00+03:00")
    if options["now"] is None:
        del options["now"]
    options.setdefault("timezone", "Europe/Istanbul")
    options.setdefault("locale", "tr-TR")

    @eks.flow(app="web-app", platform=platform, **options)
    def probe(s: eks.Session) -> Any:
        return body(s)

    return eks.plan(probe)


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def codes(report: Report) -> list[str]:
    return [issue.code for issue in report.issues]


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def nothing(_s: SessionCore) -> None:
    return None


def score_plan(registry: Registry, pins: Sequence[tuple[str, Time]], *, bpm: float | None = 120) -> Plan:
    @eks.score(fps=30, bpm=bpm)
    def ad(sc: TraceContext) -> None:
        spec = SessionSpec(key="take0", app="mobile-app", platform="ios", now="2026-01-15T09:00+03:00",
                           timezone="Europe/Istanbul", locale="tr-TR")
        resources = sc.session_resources(spec)
        with sc.container("score.take", {"session": spec}, uses=[resources["lease"], sc.session_backend(spec)],
                          flags=("take",)):
            for op, pin in pins:
                args: dict[str, Any] = {"target": by.test_id("total")}
                if op == "ui.fill":
                    args["value"] = "5"
                sc.record(op, args, uses=[resources["lease"]], pin=pin)

    return trace(registry.entry("score", "ad"), root=lambda ctx, _entry: ctx, registry=registry)


def test_pins_inside_a_take(eks_registry: Registry) -> None:
    plan = score_plan(eks_registry, [("ui.tap", t("bar:1")), ("ui.fill", t("beat:6")), ("ui.tap", t("4s"))])
    report = validate(plan)
    assert report.ok, report.text()
    assert report.issues == ()


def test_pin_order(eks_registry: Registry) -> None:
    plan = score_plan(eks_registry, [("ui.tap", t("bar:1")), ("ui.fill", t("beat:2")), ("ui.tap", t("2s"))])
    found = issues_of(validate(plan), "pin.order")
    taps = by_op(plan, "ui.tap")
    fill = by_op(plan, "ui.fill")[0]
    # bar:1 is beat 4 (2 s at 120 bpm): beat 2 comes before it and 2 s does not come after it
    assert [issue.node for issue in found] == [fill.id, taps[1].id]
    assert "'bar:1'" in found[0].message


def test_pin_order_skips_times_that_need_analysis(eks_registry: Registry) -> None:
    plan = score_plan(eks_registry, [("ui.tap", t("2s")), ("ui.tap", t("bar:0"))], bpm=None)
    report = validate(plan)
    assert issues_of(report, "pin.order") == []
    assert [issue.code for issue in report.issues] == ["time.unresolvable"]


def test_pin_op(eks_registry: Registry) -> None:
    plan = score_plan(eks_registry, [("ui.text", t("1s"))])
    [issue] = issues_of(validate(plan), "pin.op")
    assert issue.node == by_op(plan, "ui.text")[0].id
    assert "pin.op" not in codes(validate(score_plan(eks_registry, [("ui.tap", t("1s"))])))


def test_pin_outside_take() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("ui.tap", {"target": by.test_id("save")})

    plan = flow_plan(body)
    assert "pin.outside_take" not in codes(validate(plan))
    tap = by_op(plan, "ui.tap")[0]
    tap.pin = t("1s")
    [issue] = issues_of(validate(plan), "pin.outside_take")
    assert issue.node == tap.id


def test_op_platform() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("web.wheel", {})

    assert validate(flow_plan(body)).ok
    plan = flow_plan(body, platform="ios")
    [issue] = issues_of(validate(plan), "op.platform")
    assert issue.node == by_op(plan, "web.wheel")[0].id
    assert "not on ios" in issue.message


def test_op_resources(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def demo(e: TraceContext) -> None:
        e.record("ui.tap", {"target": by.test_id("save")})

    plan = trace(eks_registry.entry("edit", "demo"), root=lambda ctx, _entry: ctx, registry=eks_registry)
    report = validate(plan)
    [issue] = issues_of(report, "op.resources")
    assert "lease" in issue.message
    assert "op.platform" not in codes(report)

    def body(s: SessionCore) -> None:
        s.record_op("api.get", {"path": "/api/x"})

    flow = flow_plan(body)
    assert "op.resources" not in codes(validate(flow))
    by_op(flow, "api.get")[0].uses.clear()
    [issue] = issues_of(validate(flow), "op.resources")
    assert "backend" in issue.message


def test_recording_only_and_nested_records() -> None:
    def outside(s: SessionCore) -> None:
        s.record_op("capture.hold", {"duration": t("1s")})

    def inside(s: SessionCore) -> None:
        with s.record_container("capture.record", {"name": "demo"}):
            s.record_op("capture.hold", {"duration": t("1s")})

    def nested(s: SessionCore) -> None:
        with s.record_container("capture.record", {"name": "outer"}), \
                s.record_container("capture.record", {"name": "inner"}):
            s.record_op("ui.tap", {"target": by.test_id("save")})

    [issue] = issues_of(validate(flow_plan(outside)), "op.recording_only")
    assert "capture.hold" in issue.message
    assert validate(flow_plan(inside)).ok
    plan = flow_plan(nested)
    [issue] = issues_of(validate(plan), "record.nested")
    records = by_op(plan, "capture.record")
    assert issue.node == records[1].id
    assert records[0].id in issue.message


def test_hold_inside_a_take_is_fine(eks_registry: Registry) -> None:
    plan = score_plan(eks_registry, [("ui.tap", t("1s"))])
    take = by_op(plan, "score.take")[0]
    tap = by_op(plan, "ui.tap")[0]
    assert take.branches is not None
    take.branches["body"].append(Node(id="nzzzzzzzzzz", op="capture.hold", args={"duration": "1s"}, uses=tap.uses))
    assert "op.recording_only" not in codes(validate(plan))


def lookup(name: str) -> list[str] | None:
    return ["harman-admin", "host"] if name == "standard" else None


def test_persona_seed_with_a_lookup() -> None:
    good = flow_plan(nothing, seed="standard", as_="harman-admin")
    assert validate(good, personas=lookup).issues == ()
    plan = flow_plan(nothing, seed="missing", as_="harman-admin")
    [issue] = issues_of(validate(plan, personas=lookup), "persona.seed")
    assert issue.node is None
    assert "'missing'" in issue.message
    assert "persona.unknown" not in codes(validate(plan, personas=lookup))
    assert "persona.seed" not in codes(validate(plan))


def test_persona_unknown_with_a_lookup() -> None:
    plan = flow_plan(nothing, seed="standard", as_="ghost")
    [issue] = issues_of(validate(plan, personas=lookup), "persona.unknown")
    assert issue.node == by_op(plan, "session.as")[0].id
    assert issue.severity == "error"
    assert "'harman-admin'" in issue.message

    def switch(s: SessionCore) -> None:
        s.record_op("session.as", {"persona": "nobody"})

    plan = flow_plan(switch, seed="standard")
    [issue] = issues_of(validate(plan, personas=lookup), "persona.unknown")
    assert issue.field == "persona"


def test_persona_without_seed_is_checked_against_presets(eks_registry: Registry) -> None:
    plan = flow_plan(nothing, as_="ghost")
    report = validate(plan)
    assert report.ok
    [issue] = report.warnings
    assert issue.code == "persona.unknown"

    @eks.preset("ghost", category="persona")
    def ghost() -> PersonaPreset:
        return PersonaPreset(label="Ghost")

    assert "persona.unknown" not in {issue.code for issue in validate(plan, registry=eks_registry).issues}
    assert validate(flow_plan(nothing, as_="ghost"), registry=eks_registry).issues == ()


def test_persona_checks_with_a_registered_seed(eks_registry: Registry) -> None:
    @eks.resource("tester", persona=True)
    class Tester(eks.Resource):
        role: str = "admin"

    @eks.seed("standard", backend="eksenapi")
    def standard(d: Any) -> None:
        d.tester("harman-admin")
        d.tester("host", role="host")

    good = flow_plan(nothing, seed="standard", as_="harman-admin")
    assert eks.validate(good).issues == ()
    bad = flow_plan(nothing, seed="standard", as_="ghost")
    [issue] = issues_of(eks.validate(bad), "persona.unknown")
    assert "'host'" in issue.message
    missing = flow_plan(nothing, seed="nothing-like-it")
    [issue] = issues_of(eks.validate(missing), "persona.seed")
    assert "'standard'" in issue.message
    assert eks_registry.resource("tester").model is Tester
    assert eks_registry.entry("seed", "standard").fn is standard


def test_take_sessions_are_checked(eks_registry: Registry) -> None:
    plan = score_plan(eks_registry, [("ui.tap", t("1s"))])
    take = by_op(plan, "score.take")[0]
    session = take.args["session"]
    assert isinstance(session, dict)
    take.args["session"] = {**session, "seed": "missing", "viewport": "watch"}
    report = validate(plan, personas=lookup)
    # the take's backend resource still restores the seed the take was traced with
    assert [(issue.code, issue.node, issue.field) for issue in report.issues if issue.code != "plan.hash"] == [
        ("session.mismatch", None, None),
        ("persona.seed", take.id, "session.seed"),
        ("preset.unknown", take.id, "session.viewport"),
    ]


def test_plugin_requirements() -> None:
    plan = flow_plan(nothing)
    plan.requires.append(Requirement(plugin="acme", version="1.0"))
    [issue] = issues_of(validate(plan, versions={}), "plugin.missing")
    assert "'acme'" in issue.message
    [issue] = issues_of(validate(plan, versions={"acme": "2.0"}), "plugin.version")
    assert "2.0" in issue.message
    report = validate(plan, versions={"acme": "1.0"})
    assert "plugin.missing" not in codes(report)
    assert "plugin.version" not in codes(report)


def dynamic_flow(s: SessionCore) -> None:
    count = s.record_op("ui.count", {"target": by.test_id("row")})

    @eks.dynamic
    def per_row(n: Any = count) -> Any:
        return [s.record_op("ui.tap", {"target": by.test_id("row").nth(i)}) for i in range(n)]

    assert per_row is not None


def test_dynamic_binding() -> None:
    plan = flow_plan(dynamic_flow)
    assert validate(plan).ok, validate(plan).text()
    node = by_op(plan, "ctl.dynamic")[0]
    fn = node.args["fn"]
    assert isinstance(fn, dict)
    # range is an allowed builtin, recorded next to the by factory
    assert fn["globals"] == ["by", "range"]
    fn["globals"] = [*fn["globals"], "os"]  # type: ignore[misc]
    [issue] = issues_of(validate(plan), "dynamic.binding")
    assert issue.field is not None
    assert issue.field.startswith("fn.globals.")
    assert "'os'" in issue.message
    fn["globals"] = ["range"]
    fn["freevars"] = {"s": {"kind": "pickle", "value": None}}
    report = validate(plan)
    [issue] = issues_of(report, "dynamic.binding")
    assert issue.field == "fn.freevars.s"
    fn["freevars"] = {"limit": {"kind": "literal", "value": 3}}
    fn["globals"] = ["limit"]
    assert issues_of(validate(plan), "dynamic.binding") == []


def test_unknown_presets() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("web.viewport", {"name": "watch"})
        s.record_op("check.checkpoint", {"label": "liste", "viewport": "phone"})
        s.record_op("net.shape", {"profile": "5g"})
        s.record_op("ui.type", {"target": by.test_id("q"), "value": "x", "pace": "demo"})

    plan = flow_plan(body, viewport="watch")
    found = issues_of(validate(plan), "preset.unknown")
    assert [(issue.node, issue.field) for issue in found] == [
        (None, None),
        (by_op(plan, "web.viewport")[0].id, "name"),
        (by_op(plan, "net.shape")[0].id, "profile"),
    ]
    assert "viewport preset 'watch'" in found[0].message
    assert "network preset '5g'" in found[2].message


def test_overlay_style_preset() -> None:
    def body(s: SessionCore) -> None:
        s.record_op("overlay.show", {"overlay": {"kind": "caption", "text": "x", "style": "loud"}})
        s.record_op("overlay.show", {"overlay": {"kind": "caption", "text": "y", "style": "kb"}})

    [issue] = issues_of(validate(flow_plan(body)), "preset.unknown")
    assert issue.field == "overlay.style"


def checkpoint(checks: list[dict[str, Any]], platform: Platform = "web") -> Plan:
    def body(s: SessionCore) -> None:
        s.record_op("check.checkpoint", {"label": "liste", "checks": checks})

    return flow_plan(body, platform=platform)


def test_checkpoint_checks() -> None:
    good = checkpoint([{"kind": "layout_spill"}, {"kind": "visual", "params": {"masks": [by.test_id("clock")]}},
                       {"kind": "perf", "params": {"lcp": t("2.5s")}}])
    assert validate(good).ok, validate(good).text()
    [issue] = issues_of(validate(checkpoint([{"kind": "nope"}])), "check.kind")
    assert issue.field == "checks.0.kind"
    [issue] = issues_of(validate(checkpoint([{"kind": "loudness", "params": {"lufs": -14}}])), "check.kind")
    assert "media" in issue.message
    [issue] = issues_of(validate(checkpoint([{"kind": "layout_spill", "params": {"bogus": 1}}])), "check.args")
    assert issue.field == "checks.0.params"
    [issue] = issues_of(validate(checkpoint([{"kind": "layout_spill"}], platform="ios")), "check.platform")
    assert "not on ios" in issue.message
    found = issues_of(validate(checkpoint([{"kind": "visual", "params": {"masks": [by.css(".x")]}}],
                                          platform="ios")), "target.surface")
    assert [issue.field for issue in found] == ["checks.0.params.masks.0"]


def test_media_checks(eks_registry: Registry) -> None:
    @eks.edit(fps=30, size=(1080, 1920))
    def demo(e: TraceContext) -> None:
        song = e.record("media.source", {"uri": "file:music/song.ogg", "kind": "audio"})
        e.record("check.media", {"media": song, "kind": "loudness", "params": {"lufs": -14}})
        e.record("check.media", {"media": song, "kind": "layout_spill"})
        e.record("check.media", {"media": song, "kind": "loudness", "params": {"lufs": "loud"}})
        e.record("check.media", {"media": song, "kind": "black", "params": {"longest": "-1s"}})

    plan = trace(eks_registry.entry("edit", "demo"), root=lambda ctx, _entry: ctx, registry=eks_registry)
    report = validate(plan)
    checks = by_op(plan, "check.media")
    assert [(issue.code, issue.node) for issue in report.issues] == [
        ("check.kind", checks[1].id),
        ("check.args", checks[2].id),
        ("time.negative", checks[3].id),
    ]
    assert report.issues[2].field == "params.longest"


def test_overlay_renderers() -> None:
    def live_blur(s: SessionCore) -> None:
        s.record_op("overlay.show", {"overlay": {"kind": "blur", "anchor": by.test_id("card")}})

    assert validate(flow_plan(live_blur)).ok
    [issue] = issues_of(validate(flow_plan(live_blur, platform="ios")), "overlay.renderer")
    assert issue.field == "overlay.kind"
    assert "not on ios" in issue.message

    def annotated(kind: str) -> Callable[[SessionCore], None]:
        def body(s: SessionCore) -> None:
            shot = s.record_op("capture.shot", {"name": "liste"})
            s.ctx.record("overlay.annotate", {"artifact": shot, "overlays": [{"kind": kind, "text": "Fatura"}]})

        return body

    assert validate(flow_plan(annotated("caption"))).ok
    [issue] = issues_of(validate(flow_plan(annotated("title"))), "overlay.renderer")
    assert issue.field == "overlays.0.kind"
    assert "no screenshot renderer" in issue.message


def test_broken_seed_is_reported_not_raised(eks_registry: Registry) -> None:
    @eks.resource("tester", persona=True)
    class Tester(eks.Resource):
        role: str = "admin"

    @eks.seed("broken", backend="eksenapi")
    def broken(d: Any) -> None:
        d.tester("harman-admin")
        _ = 1 / 0

    plan = flow_plan(nothing, seed="broken", as_="harman-admin")
    report = eks.validate(plan)
    [issue] = issues_of(report, "persona.seed")
    assert "ZeroDivisionError" in issue.message
    assert "persona.unknown" not in codes(report)

    def missing_module(_name: str) -> Sequence[str] | None:
        raise ImportError("no module named 'acme_seed'")

    [issue] = issues_of(validate(plan, personas=missing_module), "persona.seed")
    assert "ImportError" in issue.message
    assert eks_registry.entry("seed", "broken").fn is broken
    assert eks_registry.resource("tester").model is Tester


def test_persona_from_an_extended_seed(eks_registry: Registry) -> None:
    @eks.resource("tester", persona=True)
    class Tester(eks.Resource):
        role: str = "admin"

    @eks.seed("standard", backend="eksenapi")
    def standard(d: Any) -> None:
        d.tester("harman-admin")

    @eks.seed("child", backend="eksenapi", base="standard")
    def child(d: Any) -> None:
        d.tester("muhasebeci")

    assert eks.validate(flow_plan(nothing, seed="child", as_="harman-admin")).issues == ()
    assert eks.validate(flow_plan(nothing, seed="child", as_="muhasebeci")).issues == ()
    [issue] = issues_of(eks.validate(flow_plan(nothing, seed="child", as_="ghost")), "persona.unknown")
    assert "'harman-admin', 'muhasebeci'" in issue.message
    assert eks_registry.entry("seed", "standard").fn is standard
    assert eks_registry.entry("seed", "child").fn is child
    assert eks_registry.resource("tester").model is Tester


def test_api_persona_is_checked_against_the_seed() -> None:
    def calls(persona: str) -> Callable[[SessionCore], None]:
        def body(s: SessionCore) -> None:
            s.record_op("api.get", {"path": "/api/me", "persona": persona})
            s.record_op("api.post", {"path": "/api/notes", "json": {"a": 1}, "persona": persona})

        return body

    good = flow_plan(calls("harman-admin"), seed="standard")
    assert validate(good, personas=lookup).issues == ()
    bad = flow_plan(calls("nobody"), seed="standard")
    found = issues_of(validate(bad, personas=lookup), "persona.unknown")
    assert [issue.node for issue in found] == [by_op(bad, "api.get")[0].id, by_op(bad, "api.post")[0].id]
    assert all(issue.field == "persona" and issue.severity == "error" for issue in found)
    assert "'harman-admin'" in found[0].message
    # without a seed an API persona is checked against the persona presets, as a warning
    unseeded = flow_plan(calls("nobody"))
    report = validate(unseeded)
    assert report.ok
    assert [issue.code for issue in report.warnings] == ["persona.unknown", "persona.unknown"]


def test_api_persona_with_a_registered_seed(eks_registry: Registry) -> None:
    @eks.resource("tester", persona=True)
    class Tester(eks.Resource):
        role: str = "admin"

    @eks.seed("standard", backend="eksenapi")
    def standard(d: Any) -> None:
        d.tester("harman-admin")

    def body(s: SessionCore) -> None:
        assert isinstance(s, eks.Session)
        s.api.get("/api/me", as_="harman-admin")
        s.api.get("/api/me", as_="ghost")

    plan = flow_plan(body, seed="standard")
    [issue] = issues_of(eks.validate(plan), "persona.unknown")
    assert issue.node == by_op(plan, "api.get")[1].id
    assert "'ghost'" in issue.message
    assert eks_registry.entry("seed", "standard").fn is standard
    assert eks_registry.resource("tester").model is Tester


class SeedFailure(Exception):
    pass


def test_seed_raising_its_own_exception_is_reported_not_raised() -> None:
    def failing(_name: str) -> Sequence[str] | None:
        raise SeedFailure("backend said no")

    plan = flow_plan(nothing, seed="standard", as_="harman-admin")
    report = validate(plan, personas=failing)
    [issue] = issues_of(report, "persona.seed")
    assert "SeedFailure: backend said no" in issue.message
    assert "persona.unknown" not in [issue.code for issue in report.issues]


def branched_score_plan(registry: Registry, then: Sequence[Time], otherwise: Sequence[Time],
                        after: Time | None = None) -> Plan:
    @eks.score(fps=30, bpm=120)
    def ad(sc: TraceContext) -> None:
        spec = SessionSpec(key="take0", app="mobile-app", platform="ios", now="2026-01-15T09:00+03:00",
                           timezone="Europe/Istanbul", locale="tr-TR")
        resources = sc.session_resources(spec)
        lease = [resources["lease"]]
        with sc.container("score.take", {"session": spec}, uses=[resources["lease"], sc.session_backend(spec)],
                          flags=("take",)):
            sc.record("ui.tap", {"target": by.test_id("open")}, uses=lease, pin=t("bar:1"))
            shown = sc.record("ui.exists", {"target": by.test_id("banner")}, uses=lease)
            with eks.when(shown):
                for pin in then:
                    sc.record("ui.tap", {"target": by.test_id("banner")}, uses=lease, pin=pin)
            if otherwise:
                with eks.otherwise():
                    for pin in otherwise:
                        sc.record("ui.tap", {"target": by.test_id("skip")}, uses=lease, pin=pin)
            if after is not None:
                sc.record("ui.tap", {"target": by.test_id("total")}, uses=lease, pin=after)

    return trace(registry.entry("score", "ad"), root=lambda ctx, _entry: ctx, registry=registry)


def test_pin_order_follows_each_branch_of_a_when(eks_registry: Registry) -> None:
    # then and else are exclusive, so both may use the same pin
    plan = branched_score_plan(eks_registry, [t("bar:2")], [t("bar:2")], after=t("bar:3"))
    assert by_op(plan, "ctl.when")[0].branches is not None
    report = validate(plan)
    assert issues_of(report, "pin.order") == [], report.text()

    # a decreasing pin inside one branch is still reported
    plan = branched_score_plan(eks_registry, [t("bar:3"), t("bar:2")], [t("bar:2")])
    [issue] = issues_of(validate(plan), "pin.order")
    branches = by_op(plan, "ctl.when")[0].branches
    assert branches is not None
    assert issue.node == branches["then"][1].id

    # after the when, a pin must come after the latest pin of either branch
    plan = branched_score_plan(eks_registry, [t("bar:2")], [t("bar:4")], after=t("bar:3"))
    [issue] = issues_of(validate(plan), "pin.order")
    assert issue.node == by_op(plan, "ui.tap")[-1].id
    assert "'bar:4'" in issue.message

    # a when without else may be skipped, so the pin before it still counts
    plan = branched_score_plan(eks_registry, [t("bar:2")], [], after=t("bar:1"))
    [issue] = issues_of(validate(plan), "pin.order")
    assert issue.node == by_op(plan, "ui.tap")[-1].id
