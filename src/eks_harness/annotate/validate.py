from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from eks_harness.annotate.measure import ResolvedElement
from eks_harness.annotate.spec import Item, Spec

DEFAULT_MATCH_PX = 2.0
DEFAULT_MIN_CONTRAST = 4.5
DEFAULT_MIN_CONTRAST_LARGE = 3.0
DEFAULT_MIN_TEXT_PX = 12.0
CROP_PADDING_CSS_PX = 48.0

EXEMPT_OVERLAP_TYPES = frozenset({"spotlight", "blur", "redact"})


@dataclass(frozen=True)
class Thresholds:
    match_px: float = DEFAULT_MATCH_PX
    min_contrast: float = DEFAULT_MIN_CONTRAST
    min_contrast_large: float = DEFAULT_MIN_CONTRAST_LARGE
    min_text_px: float = DEFAULT_MIN_TEXT_PX


@dataclass(frozen=True)
class RuleResult:
    rule: str
    ok: bool
    item_id: str | None
    detail: str
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "ok": self.ok, "itemId": self.item_id,
                "detail": self.detail, "skipped": self.skipped}


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    rules: tuple[RuleResult, ...]
    anchor_fallback: bool
    contrast: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "rules": [r.to_dict() for r in self.rules],
                "anchorFallback": self.anchor_fallback, "contrast": dict(self.contrast)}


def _item_key(item: Item, position: int) -> str:
    return item.item_id or f"item-{position}"


def _srgb_to_linear(channel: float) -> float:
    if channel <= 0.03928:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255.0 for c in rgb)
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    lighter = max(relative_luminance(first), relative_luminance(second))
    darker = min(relative_luminance(first), relative_luminance(second))
    return (lighter + 0.05) / (darker + 0.05)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _rects_overlap(first: tuple[float, float, float, float],
                   second: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _inside(inner: tuple[float, float, float, float], outer_w: float, outer_h: float) -> bool:
    x, y, w, h = inner
    return x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= outer_w and y + h <= outer_h


@dataclass
class ValidationContext:
    measured: dict[str, ResolvedElement]
    remeasured: dict[str, ResolvedElement]
    marks: dict[str, tuple[float, float, float, float]]
    image_w: float
    image_h: float
    actual_width: int
    text_colors: dict[str, str] = field(default_factory=dict)
    under_colors: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    text_sizes: dict[str, float] = field(default_factory=dict)
    text_fits: dict[str, bool] = field(default_factory=dict)
    is_large_text: dict[str, bool] = field(default_factory=dict)


def validate(spec: Spec, ctx: ValidationContext, *,
             thresholds: Thresholds | None = None) -> ValidationReport:
    limits = thresholds or Thresholds()
    rules: list[RuleResult] = []
    contrast: dict[str, float] = {}
    fallback = False
    for position, item in enumerate(spec.items, start=1):
        key = _item_key(item, position)
        anchor = item.anchor
        if anchor is not None and anchor.is_coords():
            fallback = True
        rules.extend(_validate_item(item, key, ctx, limits, contrast))
    rules.extend(check_no_overlap(spec, ctx))
    ok = all(r.ok or r.skipped for r in rules)
    return ValidationReport(ok=ok, rules=tuple(rules), anchor_fallback=fallback, contrast=contrast)


def _validate_item(item: Item, key: str, ctx: ValidationContext,
                   limits: Thresholds, contrast: dict[str, float]) -> list[RuleResult]:
    out: list[RuleResult] = []
    anchor = item.anchor
    measured = ctx.measured.get(key)
    if anchor is None:
        out.append(RuleResult("V1", True, key, "no anchor; rule does not apply.", skipped=True))
        out.append(RuleResult("V2", True, key, "no anchor; rule does not apply.", skipped=True))
        out.append(RuleResult("V3", True, key, "no expected label; rule does not apply.", skipped=True))
    elif anchor.is_coords():
        out.append(RuleResult("V1", True, key,
                              "coords anchor: authored box is the measured box; re-measure does not apply.",
                              skipped=True))
        out.append(RuleResult("V2", True, key,
                              "coords anchor: uniqueness does not apply.", skipped=True))
        out.extend(_check_label(item, key, measured))
    else:
        out.extend(_check_remeasure(item, key, ctx, limits))
        out.extend(_check_uniqueness(item, key, measured))
        out.extend(_check_label(item, key, measured))
    out.extend(_check_inside(item, key, ctx))
    out.extend(_check_size(item, key, ctx, limits))
    out.extend(_check_contrast(item, key, ctx, limits, contrast))
    out.extend(_check_wrap(item, key, ctx))
    return out


def _check_remeasure(item: Item, key: str, ctx: ValidationContext, limits: Thresholds) -> list[RuleResult]:
    first = ctx.measured.get(key)
    second = ctx.remeasured.get(key)
    if first is None or second is None:
        return [RuleResult("V1", False, key, "anchor was not measured before capture.")]
    edges = ("x", "y", "w", "h")
    deltas = {edge: abs(getattr(first.box, edge) - getattr(second.box, edge)) for edge in edges}
    worst = max(deltas.values())
    if worst <= limits.match_px:
        return [RuleResult("V1", True, key, f"re-measured box matches within {limits.match_px:g} CSS px.")]
    return [RuleResult("V1", False, key,
                       f"anchor moved before capture (worst edge {worst:.1f} px over {limits.match_px:g} px).")]


def _check_uniqueness(item: Item, key: str, measured: ResolvedElement | None) -> list[RuleResult]:
    if measured is None:
        return [RuleResult("V2", False, key, "anchor resolved to no element.")]
    if not measured.visible:
        return [RuleResult("V2", False, key, "anchor resolved to a hidden element.")]
    if measured.matches != 1:
        return [RuleResult("V2", False, key,
                           f"anchor resolved to {measured.matches} elements; exactly one is required.")]
    return [RuleResult("V2", True, key, "anchor resolves to exactly one visible element.")]


def _check_label(item: Item, key: str, measured: ResolvedElement | None) -> list[RuleResult]:
    expected = (item.label or "").strip()
    if not expected:
        if item.anchor is not None and item.anchor.kind == "role" and item.anchor.name is None:
            return [RuleResult("V3", True, key, "role anchor has no name and item has no label; skipped.", skipped=True)]
        return [RuleResult("V3", True, key, "no expected label; skipped.", skipped=True)]
    if measured is None:
        return [RuleResult("V3", False, key, "anchor was not measured; expected label cannot be checked.")]
    haystack = f"{measured.text or ''}\n{measured.name or ''}".lower()
    if expected.lower() in haystack:
        return [RuleResult("V3", True, key, "element text contains the expected label.")]
    return [RuleResult("V3", False, key,
                       f"element text does not contain {expected!r}.")]


def _check_inside(item: Item, key: str, ctx: ValidationContext) -> list[RuleResult]:
    mark = ctx.marks.get(key)
    if mark is None:
        return [RuleResult("V4", False, key, "no placed mark box; cannot check bounds.")]
    if _inside(mark, ctx.image_w, ctx.image_h):
        return [RuleResult("V4", True, key, "mark is fully inside the image.")]
    return [RuleResult("V4", False, key, "mark is clipped by the image bounds.")]


def _check_size(item: Item, key: str, ctx: ValidationContext, limits: Thresholds) -> list[RuleResult]:
    size = ctx.text_sizes.get(key)
    if size is None:
        return [RuleResult("V7", True, key, "item carries no rendered text; skipped.", skipped=True)]
    if size >= limits.min_text_px:
        return [RuleResult("V7", True, key, f"text is {size:.1f} px at display width.")]
    return [RuleResult("V7", False, key,
                       f"text is {size:.1f} px, below the {limits.min_text_px:g} px minimum.")]


def _check_contrast(item: Item, key: str, ctx: ValidationContext,
                    limits: Thresholds, contrast: dict[str, float]) -> list[RuleResult]:
    fg = ctx.text_colors.get(key)
    bg = ctx.under_colors.get(key)
    if fg is None or bg is None:
        return [RuleResult("V6", True, key, "item carries no rendered text; skipped.", skipped=True)]
    ratio = contrast_ratio(_hex_to_rgb(fg), bg)
    contrast[key] = round(ratio, 2)
    needed = limits.min_contrast_large if ctx.is_large_text.get(key) else limits.min_contrast
    if ratio >= needed:
        return [RuleResult("V6", True, key, f"contrast is {ratio:.2f}:1 (needs {needed:g}:1).")]
    return [RuleResult("V6", False, key, f"contrast is {ratio:.2f}:1 (needs {needed:g}:1).")]


def _check_wrap(item: Item, key: str, ctx: ValidationContext) -> list[RuleResult]:
    if key not in ctx.text_fits:
        return [RuleResult("V8", True, key, "item carries no wrapped text; skipped.", skipped=True)]
    if ctx.text_fits[key]:
        return [RuleResult("V8", True, key, "text fits its box with real font metrics.")]
    return [RuleResult("V8", False, key, "text overflows its box with real font metrics.")]


def _overlap_exempt(own_target: tuple[float, float, float, float] | None,
                    first: tuple[float, float, float, float],
                    second: tuple[float, float, float, float]) -> bool:
    if own_target is None:
        return False
    ox, oy, ow, oh = own_target
    x0, y0 = max(first[0], second[0]), max(first[1], second[1])
    x1, y1 = min(first[0] + first[2], second[0] + second[2]), min(first[1] + first[3], second[1] + second[3])
    return x0 >= ox and y0 >= oy and x1 <= ox + ow and y1 <= oy + oh


def check_no_overlap(spec: Spec, ctx: ValidationContext) -> list[RuleResult]:
    rules: list[RuleResult] = []
    keys = [_item_key(item, position) for position, item in enumerate(spec.items, start=1)]
    by_key = dict(zip(keys, spec.items))
    for key in keys:
        mark = ctx.marks.get(key)
        if mark is None:
            rules.append(RuleResult("V5", False, key, "no placed mark box; cannot check overlap."))
            continue
        item = by_key[key]
        own = ctx.measured[key].box.edges() if key in ctx.measured and item.anchor is not None else None
        clashes: list[str] = []
        for other in keys:
            if other == key:
                continue
            other_mark = ctx.marks.get(other)
            if other_mark is None or not _rects_overlap(mark, other_mark):
                continue
            other_item = by_key[other]
            other_own = (ctx.measured[other].box.edges()
                         if other in ctx.measured and other_item.anchor is not None else None)
            if item.type in EXEMPT_OVERLAP_TYPES and _overlap_exempt(own, mark, other_mark):
                continue
            if other_item.type in EXEMPT_OVERLAP_TYPES and _overlap_exempt(other_own, mark, other_mark):
                continue
            clashes.append(other)
        if clashes:
            rules.append(RuleResult("V5", False, key, f"mark overlaps {sorted(clashes)}."))
        else:
            rules.append(RuleResult("V5", True, key, "mark overlaps nothing."))
    return rules


TextMeasurer = Callable[[str, str, int], float]


def plan_crops(measured: dict[str, ResolvedElement],
               marks: dict[str, tuple[float, float, float, float]],
               image_w: float, image_h: float, *,
               padding_css_px: float = CROP_PADDING_CSS_PX) -> dict[str, tuple[int, int, int, int]]:
    crops: dict[str, tuple[int, int, int, int]] = {}
    for key, resolved in measured.items():
        tx, ty, tw, th = resolved.box.edges()
        x0, y0 = tx - padding_css_px, ty - padding_css_px
        x1, y1 = tx + tw + padding_css_px, ty + th + padding_css_px
        mark = marks.get(key)
        if mark is not None:
            mx, my, mw, mh = mark
            x0, y0 = min(x0, mx), min(y0, my)
            x1, y1 = max(x1, mx + mw), max(y1, my + mh)
        crops[key] = (max(0, int(x0)), max(0, int(y0)),
                      min(int(image_w), int(x1)), min(int(image_h), int(y1)))
    for key, mark in marks.items():
        if key in crops:
            continue
        mx, my, mw, mh = mark
        crops[key] = (max(0, int(mx - padding_css_px)), max(0, int(my - padding_css_px)),
                      min(int(image_w), int(mx + mw + padding_css_px)),
                      min(int(image_h), int(my + mh + padding_css_px)))
    return crops
