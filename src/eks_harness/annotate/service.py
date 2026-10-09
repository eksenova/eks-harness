from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from eks_harness.annotate.measure import Box, MeasurementProvider, ResolvedElement, stable_measure  # noqa: F401
from eks_harness.annotate.render import plan as plan_marks
from eks_harness.annotate.render import render_annotated_image, sample_under_color
from eks_harness.annotate.spec import Spec, SpecError, Style, builtin_style, parse_spec, parse_style
from eks_harness.annotate.validate import Thresholds, ValidationContext, ValidationReport, plan_crops, validate
from eks_harness.annotate.versions import AnnotationMeta


@dataclass
class AnnotateResult:
    spec: Spec
    style: Style
    png: bytes
    svg: str
    marks: dict[str, tuple[float, float, float, float]]
    sides: dict[str, str]
    measured: dict[str, ResolvedElement]
    arrow_targets: dict[str, Box]
    report: ValidationReport
    crops: dict[str, bytes] = field(default_factory=dict)
    meta: AnnotationMeta | None = None


def _item_key(item: Any, position: int) -> str:
    return item.item_id or f"item-{position}"


def resolve_style(spec: Spec, style_override: str | dict[str, Any] | Style | None,
                  custom_styles: dict[str, dict[str, Any]] | None = None) -> Style:
    if isinstance(style_override, Style):
        return style_override
    if isinstance(style_override, dict):
        return parse_style(style_override)
    name = style_override or spec.style or "kb"
    if custom_styles is not None and name in custom_styles:
        return parse_style({**custom_styles[name], "name": name})
    return builtin_style(name)


def measure_all(spec: Spec, provider: MeasurementProvider, *,
                viewport: str = "desktop") -> tuple[dict[str, ResolvedElement], dict[str, Box]]:
    from eks_harness.annotate.spec import parse_anchor

    measured: dict[str, ResolvedElement] = {}
    arrow_targets: dict[str, Box] = {}
    for position, item in enumerate(spec.items, start=1):
        key = _item_key(item, position)
        if item.anchor is not None:
            measured[key] = stable_measure(provider, item.anchor, sleep_fn=lambda _: None)
        if item.type == "arrow":
            raw_to = item.payload.get("to") or {}
            anchor = parse_anchor(raw_to, item_type="arrow", viewport=viewport)
            if anchor.is_coords():
                arrow_targets[key] = Box(x=float(anchor.x or 0), y=float(anchor.y or 0),
                                         w=float(anchor.width or 0), h=float(anchor.height or 0))
            else:
                arrow_targets[key] = stable_measure(provider, anchor, sleep_fn=lambda _: None).box
    return measured, arrow_targets


def annotate_clean_image(clean_png: bytes, spec: Spec, style: Style,
                         provider: MeasurementProvider, *,
                         recipe: dict[str, Any] | None = None,
                         clean_id: str = "",
                         authored_spec: dict[str, Any] | None = None,
                         thresholds: Thresholds | None = None,
                         assets: dict[str, tuple[bytes, str]] | None = None) -> AnnotateResult:
    from PIL import Image

    measured, arrow_targets = measure_all(spec, provider)
    clean = Image.open(io.BytesIO(clean_png)).convert("RGB")
    image_w, image_h = clean.size
    output = plan_marks(spec, measured, style, image_w, image_h,
                        assets=assets, arrow_targets=arrow_targets)
    under_colors = {key: sample_under_color(clean_png, rect) for key, rect in output.marks.items()
                    if key in output.text_colors}
    remeasured = {key: provider.resolve(item.anchor) for position, item in enumerate(spec.items, start=1)
                  for key in [_item_key(item, position)]
                  if item.anchor is not None and not item.anchor.is_coords()}
    ctx = ValidationContext(measured=measured, remeasured=remeasured, marks=output.marks,
                            image_w=float(image_w), image_h=float(image_h), actual_width=image_w,
                            text_colors=output.text_colors, under_colors=under_colors,
                            text_sizes=output.text_sizes, text_fits=output.text_fits,
                            is_large_text=output.is_large_text)
    report = validate(spec, ctx, thresholds=thresholds or Thresholds())
    rendered = render_annotated_image(clean_png, spec, measured, style,
                                      assets=assets, arrow_targets=arrow_targets)
    crops = write_crops(rendered.png, spec, measured, rendered.marks, image_w, image_h)
    boxes = {key: {"x": element.box.x, "y": element.box.y, "w": element.box.w, "h": element.box.h,
                   "scale": element.box.scale, "side": rendered.sides.get(key)}
             for position, item in enumerate(spec.items, start=1)
             for key in [_item_key(item, position)]
             for element in [measured[key]] if key in measured}
    meta = AnnotationMeta(
        clean_id=clean_id, spec=authored_spec or spec.to_dict(), normalized_spec=spec.to_dict(),
        style_name=style.name, style_snapshot=style.to_dict(), boxes=boxes,
        recipe=dict(recipe or {}), anchor_fallback=report.anchor_fallback,
        report=report.to_dict(),
        crops=[f"crops/{key}.png" for key in crops])
    return AnnotateResult(spec=spec, style=style, png=rendered.png, svg=rendered.svg,
                          marks=rendered.marks, sides=rendered.sides, measured=measured,
                          arrow_targets=arrow_targets, report=report, crops=crops, meta=meta)


def write_crops(annotated_png: bytes, spec: Spec, measured: dict[str, ResolvedElement],
                marks: dict[str, tuple[float, float, float, float]],
                image_w: int, image_h: int) -> dict[str, bytes]:
    from PIL import Image

    image = Image.open(io.BytesIO(annotated_png)).convert("RGB")
    rects = plan_crops(measured, marks, float(image_w), float(image_h))
    out: dict[str, bytes] = {}
    for position, item in enumerate(spec.items, start=1):
        key = _item_key(item, position)
        if key not in rects:
            continue
        x0, y0, x1, y1 = rects[key]
        if x1 <= x0 or y1 <= y0:
            continue
        buffer = io.BytesIO()
        image.crop((x0, y0, x1, y1)).save(buffer, format="PNG")
        out[key] = buffer.getvalue()
    return out


def parse_spec_input(spec_input: str | bytes | dict[str, Any], *,
                     viewport: str = "desktop", for_video: bool = False) -> tuple[Spec, dict[str, Any]]:
    from eks_harness.annotate.spec import loads_spec

    if isinstance(spec_input, dict):
        return parse_spec(spec_input, viewport=viewport, for_video=for_video), dict(spec_input)
    if isinstance(spec_input, (bytes, bytearray)):
        text = bytes(spec_input).decode("utf-8")
    else:
        text = spec_input
    spec = loads_spec(text, viewport=viewport, for_video=for_video)
    import json

    import yaml

    stripped = text.strip()
    if stripped[:1] in "{[":
        authored: dict[str, Any] = json.loads(text)
    else:
        authored = yaml.safe_load(text)
    if not isinstance(authored, dict):
        raise SpecError("spec must be an object.")
    return spec, authored
