from __future__ import annotations

import pytest

from eks_harness.annotate.measure import Box, FakeMeasurement, ResolvedElement, boxes_match, stable_measure
from eks_harness.annotate.spec import Anchor, loads_spec
from eks_harness.annotate.validate import (
    Thresholds,
    ValidationContext,
    check_no_overlap,
    contrast_ratio,
    plan_crops,
    validate,
)


def _element(x: float, y: float, w: float, h: float, **kwargs) -> ResolvedElement:
    return FakeMeasurement.box(x, y, w, h, **kwargs)


def _ctx(spec, measured: dict, marks: dict, remeasured: dict | None = None,
         extra: dict | None = None) -> ValidationContext:
    kwargs = dict(text_colors={}, under_colors={}, text_sizes={},
                  text_fits={}, is_large_text={})
    kwargs.update(extra or {})
    return ValidationContext(
        measured=measured, remeasured=remeasured if remeasured is not None else dict(measured),
        marks=marks, image_w=800.0, image_h=600.0, actual_width=800, **kwargs)


class _SequenceProvider:
    def __init__(self, boxes: list[Box]) -> None:
        self._boxes = list(boxes)
        self.calls = 0

    def resolve(self, anchor: Anchor) -> ResolvedElement:
        box = self._boxes[min(self.calls, len(self._boxes) - 1)]
        self.calls += 1
        return ResolvedElement(box=box, matches=1)


def test_boxes_match_tolerance() -> None:
    assert boxes_match(Box(0, 0, 10, 10), Box(0.5, 0.5, 10.5, 9.5), 1.0)
    assert not boxes_match(Box(0, 0, 10, 10), Box(0, 0, 13, 10), 2.0)


def test_stable_measure_accepts_stable_layout() -> None:
    provider = _SequenceProvider([Box(1, 2, 30, 40), Box(1, 2, 30, 40)])
    element = stable_measure(provider, Anchor(kind="text", text="Save"), sleep_fn=lambda _: None)
    assert (element.box.x, element.box.w) == (1, 30)


def test_stable_measure_retries_once_then_fails() -> None:
    provider = _SequenceProvider([Box(0, 0, 10, 10), Box(50, 50, 10, 10), Box(90, 90, 10, 10)])
    with pytest.raises(RuntimeError):
        stable_measure(provider, Anchor(kind="text", text="Save"), sleep_fn=lambda _: None)


def test_coords_anchor_skips_v1_v2_and_flags_fallback() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "highlight", "id": "h", '
                      '"anchor": {"kind": "coords", "x": 10, "y": 10, "width": 50, "height": 20}}]}')
    measured = {"h": _element(10, 10, 50, 20)}
    report = validate(spec, _ctx(spec, measured, {"h": (10, 10, 50, 20)}))
    assert report.ok
    assert report.anchor_fallback is True
    skipped = {rule.rule for rule in report.rules if rule.skipped}
    assert {"V1", "V2"} <= skipped


def test_v1_fails_when_anchor_moves() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "step", "id": "s", "anchor": {"kind": "text", "text": "Save"}}]}')
    measured = {"s": _element(10, 10, 50, 20, text="Save")}
    remeasured = {"s": _element(40, 10, 50, 20, text="Save")}
    report = validate(spec, _ctx(spec, measured, {"s": (700, 500, 28, 28)}, remeasured),
                      thresholds=Thresholds())
    assert not report.ok
    assert any(rule.rule == "V1" and not rule.ok for rule in report.rules)


def test_v2_requires_exactly_one_visible_element() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "step", "id": "s", "anchor": {"kind": "text", "text": "Save"}}]}')
    measured = {"s": _element(10, 10, 50, 20, matches=3)}
    report = validate(spec, _ctx(spec, measured, {"s": (700, 500, 28, 28)}))
    assert any(rule.rule == "V2" and not rule.ok for rule in report.rules)


def test_v3_label_substring_match() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "label", "id": "l", "anchor": {"kind": "text", "text": "Save"}, '
                      '"label": "save", "text": "Saved"}]}')
    measured = {"l": _element(10, 10, 50, 20, text="Save changes")}
    report = validate(spec, _ctx(spec, measured, {"l": (10, 60, 80, 30)}))
    assert all(rule.ok or rule.skipped for rule in report.rules if rule.rule == "V3")
    bad = loads_spec('{"version": 1, "items": ['
                     '{"type": "label", "id": "l", "anchor": {"kind": "text", "text": "Save"}, '
                     '"label": "delete", "text": "Saved"}]}')
    report = validate(bad, _ctx(bad, measured, {"l": (10, 60, 80, 30)}))
    assert any(rule.rule == "V3" and not rule.ok for rule in report.rules)


def test_v4_rejects_clipped_marks() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "step", "id": "s", "anchor": {"kind": "text", "text": "Save"}}]}')
    measured = {"s": _element(10, 10, 50, 20)}
    report = validate(spec, _ctx(spec, measured, {"s": (790, 590, 40, 40)}))
    assert any(rule.rule == "V4" and not rule.ok for rule in report.rules)


def test_v5_overlap_and_exempt_target_cover() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "step", "id": "a", "anchor": {"kind": "text", "text": "A"}}, '
                      '{"type": "step", "id": "b", "anchor": {"kind": "text", "text": "B"}}]}')
    measured = {"a": _element(10, 10, 50, 20), "b": _element(300, 300, 50, 20)}
    ctx = _ctx(spec, measured, {"a": (10, 60, 40, 40), "b": (20, 70, 40, 40)})
    assert any(rule.rule == "V5" and not rule.ok for rule in check_no_overlap(spec, ctx))
    redact_spec = loads_spec('{"version": 1, "items": ['
                             '{"type": "redact", "id": "r", "anchor": {"kind": "text", "text": "A"}}]}')
    ctx = _ctx(redact_spec, {"r": _element(10, 10, 50, 20)},
               {"r": (10, 10, 50, 20)})
    assert all(rule.ok for rule in check_no_overlap(redact_spec, ctx))


def test_v6_contrast_math() -> None:
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, rel=0.01)
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "step", "id": "s", "anchor": {"kind": "text", "text": "Save"}}]}')
    measured = {"s": _element(10, 10, 50, 20)}
    ctx = _ctx(spec, measured, {"s": (700, 500, 28, 28)},
               extra={"text_colors": {"s": "#ffffff"}, "under_colors": {"s": (255, 255, 255)},
                      "text_sizes": {"s": 16.0}, "is_large_text": {"s": False}})
    report = validate(spec, ctx)
    assert any(rule.rule == "V6" and not rule.ok for rule in report.rules)
    assert report.contrast["s"] == pytest.approx(1.0)


def test_v7_minimum_size() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "label", "id": "l", "anchor": {"kind": "text", "text": "Save"}, '
                      '"text": "Saved"}]}')
    measured = {"l": _element(10, 10, 50, 20)}
    ctx = _ctx(spec, measured, {"l": (10, 60, 80, 30)}, extra={"text_sizes": {"l": 8.0}})
    assert any(rule.rule == "V7" and not rule.ok for rule in validate(spec, ctx).rules)


def test_v8_overflow_fails() -> None:
    spec = loads_spec('{"version": 1, "items": ['
                      '{"type": "callout", "id": "c", "anchor": {"kind": "text", "text": "Save"}, '
                      '"text": "hello"}]}')
    measured = {"c": _element(10, 10, 50, 20)}
    ctx = _ctx(spec, measured, {"c": (10, 60, 120, 40)}, extra={"text_fits": {"c": False}})
    assert any(rule.rule == "V8" and not rule.ok for rule in validate(spec, ctx).rules)


def test_plan_crops_cover_target_and_mark() -> None:
    measured = {"a": _element(100, 100, 50, 20)}
    crops = plan_crops(measured, {"a": (200, 200, 60, 30)}, 800.0, 600.0)
    x0, y0, x1, y1 = crops["a"]
    assert (x0, y0) == (52, 52)
    assert (x1, y1) == (260, 230)
