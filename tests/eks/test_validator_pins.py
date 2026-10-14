from __future__ import annotations

from typing import Any

import pytest

import eks
from eks import by, t
from eks.planning import trace
from eks.tracing import TraceContext, TraceError
from eks_harness.engine.validator import ISSUE_CODES, Issue, Report, validate
from eks_harness.ir.hashing import digest, hash_plan
from eks_harness.ir.plan import Node, Plan, SessionSpec
from eks_harness.ir.registry import Registry


def issues_of(report: Report, code: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.code == code]


def by_op(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def taps(plan: Plan) -> list[Node]:
    return by_op(plan, "ui.tap")


def test_pin_codes_are_registered() -> None:
    assert "take span" in ISSUE_CODES["pin.span"]
    assert "eks.each" in ISSUE_CODES["pin.loop"]
    assert "bpy" in ISSUE_CODES["dynamic.binding"]


def test_pins_outside_the_take_span_are_rejected(eks_registry: Registry) -> None:
    # the reproduction: a 2s..4s take inside a 0s..8s window accepted pins at 1s, 6s and 30s
    @eks.score(fps=30, window=t.span(t("0s"), t("8s")))
    def spanned(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", span=t.span(t("2s"), t("4s")))
        with take.flow() as s:
            s.ui.tap("#early", at=t("1s"))
            s.ui.tap("#inside", at=t("3s"))
            s.ui.tap("#late", at=t("6s"))
            s.ui.tap("#far", at=t("30s"))

    plan = eks.plan(spanned)
    report = eks.validate(plan)
    assert not report.ok
    early, inside, late, far = taps(plan)
    found = issues_of(report, "pin.span")
    assert [issue.node for issue in found] == [early.id, late.id, far.id]
    assert inside.id not in {issue.node for issue in report.issues}
    assert "span '2s'..'4s'" in found[0].message
    assert all(issue.severity == "error" for issue in found)


def test_pins_on_the_span_ends_are_inside(eks_registry: Registry) -> None:
    @eks.score(fps=30, window=t.span(t("0s"), t("8s")))
    def edges(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", span=t.span(t("2s"), t("4s")))
        with take.flow() as s:
            s.ui.tap("#start", at=t("2s"))
            s.ui.tap("#end", at=t("4s"))

    assert issues_of(eks.validate(eks.plan(edges)), "pin.span") == []


def test_without_a_span_pins_stay_inside_the_score_window(eks_registry: Registry) -> None:
    @eks.score(fps=30, window=t.span(t("1s"), t("8s")))
    def windowed(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#before", at=t("500ms"))
            s.ui.tap("#within", at=t("5s"))
            s.ui.tap("#after", at=t("9s"))

    plan = eks.plan(windowed)
    before, _within, after = taps(plan)
    found = issues_of(eks.validate(plan), "pin.span")
    assert [issue.node for issue in found] == [before.id, after.id]
    assert "score window" in found[0].message


def test_without_a_span_or_window_pins_stay_inside_the_duration(eks_registry: Registry) -> None:
    # the reproduction: duration=8s and no span validated a pin at 20s
    @eks.score(fps=30, duration=t("8s"))
    def timed(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#ok", at=t("7s"))
            s.ui.tap("#late", at=t("20s"))

    plan = eks.plan(timed)
    assert plan.clock.duration == t("8s")
    assert plan.clock.window is None
    report = eks.validate(plan)
    assert not report.ok
    [issue] = issues_of(report, "pin.span")
    assert issue.node == taps(plan)[1].id
    assert "clock duration '8s'" in issue.message


def test_beat_pins_resolve_on_the_static_tempo(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120, window=t.span(t("0s"), t("4s")))
    def beats(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#third", at=t("beat:2"))
            s.ui.tap("#bar", at=t("bar:3"))

    plan = eks.plan(beats)
    [issue] = issues_of(eks.validate(plan), "pin.span")
    assert issue.node == taps(plan)[1].id


def test_unresolvable_pins_and_open_clocks_are_skipped(eks_registry: Registry) -> None:
    @eks.score(fps=30)
    def open_clock(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#far", at=t("90s"))

    assert issues_of(eks.validate(eks.plan(open_clock)), "pin.span") == []

    @eks.score(fps=30, window=t.span(t("0s"), t("8s")))
    def needs_tempo(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#beat", at=t("beat:40"))

    assert issues_of(eks.validate(eks.plan(needs_tempo)), "pin.span") == []


def test_at_inside_a_runtime_each_of_a_take_raises(eks_registry: Registry) -> None:
    # the reproduction: a pinned tap inside `with eks.each(rows)` would land every row on the same time
    @eks.score(fps=30, bpm=120)
    def looped(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            rows = s.record_op("api.get", {"path": "/api/rows"})
            with eks.each(rows.items) as row:
                s.ui.tap(f"#row-{row.id}", at=t("beat:2"))

    with pytest.raises(TraceError, match="runtime eks.each") as caught:
        eks.plan(looped)
    assert "same time" in str(caught.value)
    assert "outside the loop" in str(caught.value)


def test_at_inside_a_literal_each_is_unrolled_and_ordered(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def unrolled(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s, eks.each(["a", "b"]) as name:
            s.ui.tap(f"#row-{name}", at=t("beat:2"))

    plan = eks.plan(unrolled)
    assert by_op(plan, "ctl.each") == []
    first, second = taps(plan)
    assert first.pin == second.pin == t("beat:2")
    report = eks.validate(plan)
    assert issues_of(report, "pin.loop") == []
    [issue] = issues_of(report, "pin.order")
    assert issue.node == second.id


def test_unpinned_steps_inside_a_runtime_each_of_a_take_are_fine(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def looped(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#open", at=t("beat:1"))
            rows = s.record_op("api.get", {"path": "/api/rows"})
            with eks.each(rows.items) as row:
                s.ui.tap(f"#row-{row.id}")

    report = eks.validate(eks.plan(looped))
    assert issues_of(report, "pin.loop") == []


def hand_built_loop(registry: Registry) -> Plan:
    @eks.score(fps=30, bpm=120)
    def ad(sc: TraceContext) -> None:
        spec = SessionSpec(key="take0", app="web-app", platform="web", now="2026-01-15T09:00+03:00",
                           timezone="Europe/Istanbul", locale="tr-TR")
        resources = sc.session_resources(spec)
        uses = [resources["lease"], sc.session_backend(spec)]
        with sc.container("score.take", {"session": spec}, uses=uses, flags=("take",)):
            rows = sc.record("api.get", {"path": "/api/rows"}, uses=[sc.session_backend(spec)])
            with sc.container("ctl.each", {"over": rows}, each=True):
                sc.record("ui.tap", {"target": by.test_id("row")}, uses=[resources["lease"]], pin=t("beat:2"))

    return trace(registry.entry("score", "ad"), root=lambda ctx, _entry: ctx, registry=registry)


def test_a_hand_built_pin_inside_ctl_each_is_rejected(eks_registry: Registry) -> None:
    plan = hand_built_loop(eks_registry)
    report = validate(plan)
    [issue] = issues_of(report, "pin.loop")
    [loop] = by_op(plan, "ctl.each")
    assert issue.node == taps(plan)[0].id
    assert repr(loop.id) in issue.message
    assert not report.ok


def test_at_inside_a_dynamic_function_of_a_take_is_rejected(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def dynamic_pins(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                return [s.ui.tap(by.test_id("row").nth(i), at=t("beat:2")) for i in range(n)]

            assert per_row is not None

    plan = eks.plan(dynamic_pins)
    report = eks.validate(plan)
    [issue] = issues_of(report, "pin.loop")
    [node] = by_op(plan, "ctl.dynamic")
    assert issue.node == node.id
    assert issue.field == "fn.source"
    assert not report.ok


def test_at_inside_a_dynamic_function_outside_a_take_is_not_a_loop_pin(eks_registry: Registry) -> None:
    @eks.flow(app="web-app", platform="web", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul",
              locale="tr-TR")
    def plain(s: eks.Session) -> None:
        count = s.record_op("ui.count", {"target": by.test_id("row")})

        @eks.dynamic
        def per_row(n: Any = count) -> Any:
            return [s.ui.tap(by.test_id("row").nth(i), at=t("1s")) for i in range(n)]

        assert per_row is not None

    assert issues_of(eks.validate(eks.plan(plain)), "pin.loop") == []


def test_a_take_span_past_the_score_window_is_cut_to_it(eks_registry: Registry) -> None:
    # the reproduction: a 2s..20s take on a 0s..8s score clock accepted a pin at 15s
    @eks.score(fps=30, window=t.span(t("0s"), t("8s")))
    def overhanging(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", span=t.span(t("2s"), t("20s")))
        with take.flow() as s:
            s.ui.tap("#inside", at=t("5s"))
            s.ui.tap("#past", at=t("15s"))

    plan = eks.plan(overhanging)
    report = eks.validate(plan)
    assert not report.ok
    [take] = by_op(plan, "score.take")
    [spanned] = issues_of(report, "take.span")
    assert spanned.node == take.id
    assert spanned.field == "span"
    assert "score window" in spanned.message
    [pinned] = issues_of(report, "pin.span")
    assert pinned.node == taps(plan)[1].id
    assert "cut to the score window" in pinned.message


def test_a_take_span_inside_the_clock_is_not_reported(eks_registry: Registry) -> None:
    @eks.score(fps=30, duration=t("8s"))
    def fitting(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", span=t.span(t("2s"), t("end")))
        with take.flow() as s:
            s.ui.tap("#a", at=t("end"))

    report = eks.validate(eks.plan(fitting))
    assert issues_of(report, "take.span") == []
    assert issues_of(report, "pin.span") == []


@pytest.mark.parametrize("clock", [{"duration": t("8s")}, {"window": t.span(t("0s"), t("8s"))}])
def test_end_anchored_pins_resolve_against_the_clock_end(clock: dict[str, Any], eks_registry: Registry) -> None:
    # the reproduction: at=t("end") + t("5s") on an 8s clock validated because 'end' never resolved
    @eks.score(fps=30, **clock)
    def anchored(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#last", at=t("end") - t("1s"))
            s.ui.tap("#past", at=t("end") + t("5s"))

    plan = eks.plan(anchored)
    report = eks.validate(plan)
    assert not report.ok
    [issue] = issues_of(report, "pin.span")
    assert issue.node == taps(plan)[1].id


def test_end_anchored_pins_take_part_in_pin_order(eks_registry: Registry) -> None:
    @eks.score(fps=30, duration=t("8s"))
    def backwards(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#late", at=t("end") - t("1s"))
            s.ui.tap("#early", at=t("2s"))

    plan = eks.plan(backwards)
    [issue] = issues_of(eks.validate(plan), "pin.order")
    assert issue.node == taps(plan)[1].id


def test_at_through_a_splatted_mapping_in_a_dynamic_function_is_rejected(eks_registry: Registry) -> None:
    # the reproduction: **{"at": t("beat:2")} slipped past the literal at= scan
    @eks.score(fps=30, bpm=120)
    def splatted(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                return [s.ui.tap(by.test_id("row").nth(i), **{"at": t("beat:2")})  # noqa: PIE804  # pyright: ignore[reportArgumentType]
                        for i in range(n)]

            assert per_row is not None

    plan = eks.plan(splatted)
    report = eks.validate(plan)
    assert not report.ok
    [issue] = issues_of(report, "pin.loop")
    assert issue.field == "fn.source"
    assert "** mapping" in issue.message


def test_opaque_keyword_splats_in_a_take_dynamic_function_are_rejected(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def opaque(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                options: dict[str, Any] = {"force": True}
                return [s.ui.tap(by.test_id("row").nth(i), **options) for i in range(n)]

            assert per_row is not None

    [issue] = issues_of(eks.validate(eks.plan(opaque)), "pin.loop")
    assert "may carry at=" in issue.message


def test_literal_splats_without_at_are_fine(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def literal(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                return [s.ui.tap(by.test_id("row").nth(i), **{"force": True})  # noqa: PIE804  # pyright: ignore[reportArgumentType]
                        for i in range(n)]

            assert per_row is not None

    assert issues_of(eks.validate(eks.plan(literal)), "pin.loop") == []


def test_take_span_code_is_registered() -> None:
    assert "score window" in ISSUE_CODES["take.span"]


def test_pins_before_zero_on_an_open_clock_are_rejected(eks_registry: Registry) -> None:
    # the reproduction: with no window and no duration a pin at -3s and a span from -5s validated
    @eks.score(fps=30)
    def open_negative(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            s.ui.tap("#before-zero", at=t("0s") - t("3s"))
            s.ui.tap("#late", at=t("90s"))

    plan = eks.plan(open_negative)
    assert plan.clock.window is None
    assert plan.clock.duration is None
    report = eks.validate(plan)
    assert not report.ok
    [issue] = issues_of(report, "pin.span")
    assert issue.node == taps(plan)[0].id
    assert "starts at 0" in issue.message

    @eks.score(fps=30)
    def open_negative_span(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app", span=t.span(t("0s") - t("5s"), t("2s")))
        with take.flow() as s:
            s.ui.tap("#early", at=t("0s") - t("4s"))
            s.ui.tap("#inside", at=t("1s"))

    plan = eks.plan(open_negative_span)
    report = eks.validate(plan)
    assert not report.ok
    [span] = issues_of(report, "take.span")
    assert span.field == "span"
    assert "starts at 0" in span.message
    [pin] = issues_of(report, "pin.span")
    assert pin.node == taps(plan)[0].id


def hand_built_take_in_each(registry: Registry) -> Plan:
    @eks.score(fps=30, bpm=120)
    def ad(sc: TraceContext) -> None:
        spec = SessionSpec(key="take0", app="web-app", platform="web", now="2026-01-15T09:00+03:00",
                           timezone="Europe/Istanbul", locale="tr-TR")
        resources = sc.session_resources(spec)
        uses = [resources["lease"], sc.session_backend(spec)]
        rows = sc.record("api.get", {"path": "/api/rows"}, uses=[sc.session_backend(spec)])
        with sc.container("ctl.each", {"over": rows}, each=True), sc.container(
                "score.take", {"session": spec}, uses=uses, flags=("take",)):
            sc.record("ui.tap", {"target": by.test_id("row")}, uses=[resources["lease"]], pin=t("beat:2"))

    return trace(registry.entry("score", "ad"), root=lambda ctx, _entry: ctx, registry=registry)


def test_a_hand_built_take_inside_a_runtime_each_is_rejected(eks_registry: Registry) -> None:
    # the reproduction: score.take inside ctl.each with a pinned tap validated with no issues
    plan = hand_built_take_in_each(eks_registry)
    report = validate(plan)
    assert not report.ok
    [loop] = by_op(plan, "ctl.each")
    [take] = by_op(plan, "score.take")
    [placement] = issues_of(report, "take.placement")
    assert placement.node == take.id
    assert repr(loop.id) in placement.message
    [pinned] = issues_of(report, "pin.loop")
    assert pinned.node == taps(plan)[0].id
    assert "take.placement" in ISSUE_CODES


def test_takes_at_the_top_of_a_score_are_placed_correctly(eks_registry: Registry) -> None:
    @eks.score(fps=30, bpm=120)
    def two(sc: eks.Score) -> None:
        for app in ("web-app", "web-app"):
            take = sc.take(platform="web", app=app)
            with take.flow() as s:
                s.ui.tap("#go", at=t("beat:1"))

    assert issues_of(eks.validate(eks.plan(two)), "take.placement") == []


def test_a_pinning_macro_inside_a_take_dynamic_function_is_rejected(eks_registry: Registry) -> None:
    # the reproduction: a registered macro that pins, called per row from an eks.dynamic of a take, validated
    @eks.macro(namespace="probe")
    def pinned_tap(s: eks.Session) -> None:
        s.ui.tap("#row", at=t("2s"))

    @eks.macro(namespace="probe")
    def plain_tap(s: eks.Session) -> None:
        s.ui.tap("#row")

    @eks.score(fps=30)
    def macro_pins(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                return [s.probe.pinned_tap() for i in range(n)]

            @eks.dynamic
            def per_plain(n: Any = count) -> Any:
                return [s.probe.plain_tap() for i in range(n)]

            assert per_row is not None
            assert per_plain is not None

    plan = eks.plan(macro_pins)
    report = eks.validate(plan)
    assert not report.ok
    pinned, _plain = by_op(plan, "ctl.dynamic")
    [issue] = issues_of(report, "pin.loop")
    assert issue.node == pinned.id
    assert issue.field == "fn.source"
    assert "macro 'probe.pinned_tap'" in issue.message
    assert "pins a step with at=" in issue.message


def test_a_macro_pinning_through_a_helper_and_a_nested_macro_is_rejected(eks_registry: Registry) -> None:
    def pin_it(s: eks.Session) -> None:
        s.ui.tap("#row", at=t("2s"))

    @eks.macro(namespace="probe")
    def inner(s: eks.Session) -> None:
        pin_it(s)

    @eks.macro(namespace="probe")
    def outer(s: eks.Session) -> None:
        s.probe.inner()

    assert inner is not None

    @eks.score(fps=30)
    def nested(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                return [s.probe.outer() for i in range(n)]

            assert per_row is not None

    [issue] = issues_of(eks.validate(eks.plan(nested)), "pin.loop")
    assert "macro 'probe.outer'" in issue.message
    assert "pin_it" in issue.message
    assert outer is not None


def take_dynamic_plan() -> Plan:
    @eks.score(fps=30)
    def plain(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                return [s.ui.tap(by.test_id("row").nth(i)) for i in range(n)]

            assert per_row is not None

    return eks.plan(plain)


def with_dynamic_source(plan: Plan, source: str) -> Plan:
    data = plan.model_dump(mode="json")
    stack: list[dict[str, Any]] = list(data["nodes"])
    while stack:
        node = stack.pop()
        if node["op"] == "ctl.dynamic":
            node["args"]["fn"]["source"] = source
            node["args"]["fn"]["hash"] = digest(source.encode("utf-8"))
        for children in (node.get("branches") or {}).values():
            stack.extend(children)
    return hash_plan(Plan.model_validate(data))


def test_pin_keyword_through_the_trace_context_is_rejected(eks_registry: Registry) -> None:
    # the reproduction: s.ctx.record("ui.tap", {...}, pin=t("2s")) inside a take's eks.dynamic validated
    @eks.score(fps=30)
    def through_ctx(sc: eks.Score) -> None:
        take = sc.take(platform="web", app="web-app")
        with take.flow() as s:
            count = s.record_op("ui.count", {"target": by.test_id("row")})

            @eks.dynamic
            def per_row(n: Any = count) -> Any:
                return [s.ctx.record("ui.tap", {"target": "#row"}, pin=t("2s")) for i in range(n)]

            assert per_row is not None

    with pytest.raises(TraceError, match="reaches 'ctx'"):
        eks.plan(through_ctx)

    plan = take_dynamic_plan()
    assert issues_of(eks.validate(plan), "pin.loop") == []
    edited = with_dynamic_source(plan, (
        "def per_row(n=1):\n"
        "    return [s.record_op('ui.tap', {'target': '#row'}, at=None) for i in range(n)] + "
        "[s.ui.tap('#row', pin=t('2s'))]\n"
    ))
    found = issues_of(eks.validate(edited), "pin.loop")
    assert ["with at=" in issue.message for issue in found] == [True, False]
    assert "with pin=" in found[1].message
