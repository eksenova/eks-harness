from __future__ import annotations

from eks_harness.annotate.video import HIGHLIGHT_SEC, TimelineStep, drawbox_filters, plan_burn_in


def steps(*times: float) -> list[TimelineStep]:
    return [TimelineStep(t=t, action="click") for t in times]


def test_highlight_starts_at_the_step_and_ends_shortly_after() -> None:
    plan = plan_burn_in(steps(1.0, 6.0), [], duration=10.0,
                        boxes_by_step={0: {"save": (10, 20, 100, 40)}})
    assert len(plan.highlights) == 1
    window = plan.highlights[0]
    assert window.start == 1.0
    assert window.end == 1.0 + HIGHLIGHT_SEC


def test_highlight_never_reaches_the_next_step() -> None:
    plan = plan_burn_in(steps(1.0, 1.2, 5.0), [], duration=10.0,
                        boxes_by_step={0: {"a": (0, 0, 10, 10)}, 1: {"b": (0, 0, 10, 10)}})
    first, second = plan.highlights
    assert first.end == 1.2
    assert second.start == 1.2
    assert second.end == 1.2 + HIGHLIGHT_SEC


def test_highlight_is_clipped_to_the_clip() -> None:
    plan = plan_burn_in(steps(9.8), [], duration=10.0, boxes_by_step={0: {"a": (0, 0, 10, 10)}})
    assert plan.highlights[0].end == 10.0
    assert plan_burn_in(steps(10.0), [], duration=10.0, boxes_by_step={0: {"a": (0, 0, 10, 10)}}).highlights == []


def test_drawbox_window_is_end_exclusive() -> None:
    plan = plan_burn_in(steps(2.0), [], duration=10.0, boxes_by_step={0: {"a": (1, 2, 3, 4)}})
    (drawbox,) = drawbox_filters(plan)
    assert "enable='gte(t,2.000)*lt(t,2.500)'" in drawbox
