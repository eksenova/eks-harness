from __future__ import annotations

import base64
import io
import xml.sax.saxutils as saxutils
from dataclasses import dataclass, field
from pathlib import Path

from eks_harness.annotate.measure import Box, ResolvedElement
from eks_harness.annotate.spec import Item, Spec, Style

FONTS_DIR = Path(__file__).resolve().parent / "fonts"
REGULAR_FONT = FONTS_DIR / "IBMPlexSans-Regular.ttf"
SEMIBOLD_FONT = FONTS_DIR / "IBMPlexSans-SemiBold.ttf"

GAP_PX = 8.0


@dataclass(frozen=True)
class PlacedMark:
    rect: tuple[float, float, float, float]
    side: str
    svg: str


@dataclass
class RenderPlan:
    marks: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    sides: dict[str, str] = field(default_factory=dict)
    fragments: list[str] = field(default_factory=list)
    text_colors: dict[str, str] = field(default_factory=dict)
    text_sizes: dict[str, float] = field(default_factory=dict)
    text_fits: dict[str, bool] = field(default_factory=dict)
    is_large_text: dict[str, bool] = field(default_factory=dict)
    blur_boxes: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    redact_boxes: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    blur_radius: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RenderResult:
    png: bytes
    svg: str
    marks: dict[str, tuple[float, float, float, float]]
    sides: dict[str, str]
    text_colors: dict[str, str]
    text_sizes: dict[str, float]
    text_fits: dict[str, bool]
    is_large_text: dict[str, bool]


def bundled_font_files() -> list[str]:
    return [str(REGULAR_FONT), str(SEMIBOLD_FONT)]


def _pil_font(path: Path, size_px: float):
    from PIL import ImageFont

    return ImageFont.truetype(str(path), max(1, int(round(size_px))))


def measure_text_width(text: str, font_path: str, size_px: float) -> float:
    font = _pil_font(Path(font_path), size_px)
    box = font.getbbox(text)
    return float(box[2] - box[0])


def default_text_measurer(text: str, font_path: str, size_px: float) -> float:
    return measure_text_width(text, font_path, size_px)


def regular_font_path(style: Style) -> str:
    candidate = FONTS_DIR / Path(style.regular).name
    if candidate.is_file():
        return str(candidate)
    custom = Path(style.regular)
    if custom.is_file():
        return str(custom)
    return str(REGULAR_FONT)


def semibold_font_path(style: Style) -> str:
    candidate = FONTS_DIR / Path(style.semibold).name
    if candidate.is_file():
        return str(candidate)
    custom = Path(style.semibold)
    if custom.is_file():
        return str(custom)
    return str(SEMIBOLD_FONT)


def wrap_text(text: str, font_path: str, size_px: float, max_width: float) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if measure_text_width(trial, font_path, size_px) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _escape(text: str) -> str:
    return saxutils.escape(text)


def _free_space(target: tuple[float, float, float, float], img_w: float, img_h: float) -> dict[str, float]:
    x, y, w, h = target
    return {
        "top": max(0.0, y) * img_w,
        "bottom": max(0.0, img_h - (y + h)) * img_w,
        "left": max(0.0, x) * img_h,
        "right": max(0.0, img_w - (x + w)) * img_h,
    }


def choose_side(target: tuple[float, float, float, float], img_w: float, img_h: float,
                *, prefer: str | None = None) -> str:
    space = _free_space(target, img_w, img_h)
    if prefer is not None and space.get(prefer, 0) > 0:
        return prefer
    return max(("top", "bottom", "left", "right"), key=lambda side: space[side])


def _clamp_inside(rect: tuple[float, float, float, float], img_w: float, img_h: float
                  ) -> tuple[float, float, float, float]:
    x, y, w, h = rect
    x = min(max(0.0, x), max(0.0, img_w - w))
    y = min(max(0.0, y), max(0.0, img_h - h))
    return (x, y, w, h)


def badge_position(target: tuple[float, float, float, float], size: float,
                   img_w: float, img_h: float) -> tuple[tuple[float, float, float, float], str]:
    return badge_box_position(target, size, size, img_w, img_h)


def badge_box_position(target: tuple[float, float, float, float], box_w: float, box_h: float,
                       img_w: float, img_h: float) -> tuple[tuple[float, float, float, float], str]:
    x, y, w, h = target
    gap = GAP_PX
    candidates = [
        ("top", (x - gap, y - box_h - gap)),
        ("top", (x, y - box_h - gap)),
        ("left", (x - box_w - gap, y - gap)),
        ("right", (x + w + gap, y - gap)),
        ("bottom", (x - gap, y + h + gap)),
        ("left", (x - box_w - gap, y)),
        ("right", (x + w + gap, y)),
        ("bottom", (x, y + h + gap)),
    ]
    for side, (bx, by) in candidates:
        if bx >= 0 and by >= 0 and bx + box_w <= img_w and by + box_h <= img_h:
            return (bx, by, box_w, box_h), side
    return _clamp_inside((x - gap, y - box_h - gap, box_w, box_h), img_w, img_h), "top"


def callout_position(target: tuple[float, float, float, float], box_w: float, box_h: float,
                     img_w: float, img_h: float) -> tuple[tuple[float, float, float, float], str]:
    x, y, w, h = target
    gap = GAP_PX * 2
    side = choose_side(target, img_w, img_h)
    if side == "top":
        rect = (x + w / 2 - box_w / 2, y - box_h - gap, box_w, box_h)
    elif side == "bottom":
        rect = (x + w / 2 - box_w / 2, y + h + gap, box_w, box_h)
    elif side == "left":
        rect = (x - box_w - gap, y + h / 2 - box_h / 2, box_w, box_h)
    else:
        rect = (x + w + gap, y + h / 2 - box_h / 2, box_w, box_h)
    return _clamp_inside(rect, img_w, img_h), side


def apply_forced(rect: tuple[float, float, float, float], side: str, dx: float, dy: float,
                 img_w: float, img_h: float) -> tuple[tuple[float, float, float, float], str]:
    x, y, w, h = rect
    return _clamp_inside((x + dx, y + dy, w, h), img_w, img_h), side


def _target_rect(box: Box) -> tuple[float, float, float, float]:
    return (box.x, box.y, box.w, box.h)


def plan(spec: Spec, measured: dict[str, ResolvedElement], style: Style,
         image_w: int, image_h: int, *,
         assets: dict[str, tuple[bytes, str]] | None = None,
         arrow_targets: dict[str, Box] | None = None) -> RenderPlan:
    from eks_harness.annotate.validate import _item_key as key_of

    output = RenderPlan()
    img_w, img_h = float(image_w), float(image_h)
    for position, item in enumerate(spec.items, start=1):
        key = key_of(item, position)
        resolved = measured.get(key)
        if resolved is None and item.anchor is not None and item.anchor.is_coords():
            anchor = item.anchor
            resolved = ResolvedElement(
                box=Box(x=float(anchor.x or 0), y=float(anchor.y or 0),
                        w=float(anchor.width or 0), h=float(anchor.height or 0), scale=1.0),
                matches=1)
        _plan_item(item, key, resolved, output, style, img_w, img_h,
                   assets or {}, (arrow_targets or {}).get(key))
    return output


def _plan_item(item: Item, key: str, resolved: ResolvedElement | None, output: RenderPlan,
               style: Style, img_w: float, img_h: float,
               assets: dict[str, tuple[bytes, str]], arrow_target: Box | None) -> None:
    colors = style.colors
    if item.type == "caption":
        bar_h = style.scaled(style.caption_bar_height, int(img_w))
        font_size = style.scaled(style.caption_font_size, int(img_w))
        font_path = regular_font_path(style)
        max_width = img_w - 2 * style.scaled(style.callout_padding, int(img_w))
        lines = wrap_text(item.payload["text"], font_path, font_size, max_width)
        fits = len(lines) <= 2
        output.text_fits[key] = fits
        shown = lines[:2]
        y = 0.0 if item.payload.get("position", "bottom") == "top" else img_h - bar_h
        rect = (0.0, y, img_w, bar_h)
        output.marks[key] = rect
        output.sides[key] = str(item.payload.get("position", "bottom"))
        output.text_colors[key] = colors["captionText"]
        output.text_sizes[key] = font_size
        output.is_large_text[key] = font_size >= 24
        line_h = font_size * 1.25
        start_y = y + (bar_h - line_h * len(shown)) / 2 + font_size
        texts = "".join(
            f'<text x="{img_w / 2}" y="{start_y + n * line_h}" text-anchor="middle" '
            f'font-family="{_escape(style.family)}" font-weight="400" font-size="{font_size:.1f}" '
            f'fill="{colors["captionText"]}">{_escape(line)}</text>'
            for n, line in enumerate(shown))
        output.fragments.append(
            f'<rect x="0" y="{y:.1f}" width="{img_w:.1f}" height="{bar_h:.1f}" fill="{colors["captionBar"]}"/>'
            + texts)
        return
    if item.type == "title":
        rect = (0.0, 0.0, img_w, img_h)
        output.marks[key] = rect
        output.sides[key] = "full"
        title_size = style.scaled(style.title_font_size, int(img_w))
        sub_size = style.scaled(style.title_sub_font_size, int(img_w))
        output.text_colors[key] = colors["titleText"]
        output.text_sizes[key] = title_size
        output.is_large_text[key] = True
        output.text_fits[key] = True
        subtitle = item.payload.get("subtitle", "")
        sub_svg = (f'<text x="{img_w / 2}" y="{img_h / 2 + title_size}" text-anchor="middle" '
                   f'font-family="{_escape(style.family)}" font-weight="400" font-size="{sub_size:.1f}" '
                   f'fill="{colors["titleText"]}">{_escape(subtitle)}</text>' if subtitle else "")
        output.fragments.append(
            f'<rect x="0" y="0" width="{img_w:.1f}" height="{img_h:.1f}" fill="{colors["titleCard"]}"/>'
            f'<text x="{img_w / 2}" y="{img_h / 2}" text-anchor="middle" '
            f'font-family="{_escape(style.family)}" font-weight="600" font-size="{title_size:.1f}" '
            f'fill="{colors["titleText"]}">{_escape(item.payload["text"])}</text>' + sub_svg)
        return
    if resolved is None:
        return
    target = _target_rect(resolved.box)
    if item.type == "step":
        size = style.scaled(style.badge_size, int(img_w))
        font_size = style.scaled(style.badge_font_size, int(img_w))
        rect, side = badge_position(target, size, img_w, img_h)
        if item.placement.mode == "force" and item.placement.side:
            rect, side = apply_forced(rect, item.placement.side, item.placement.dx, item.placement.dy, img_w, img_h)
        output.marks[key] = rect
        output.sides[key] = side
        output.text_colors[key] = colors["badgeText"]
        output.text_sizes[key] = font_size
        output.is_large_text[key] = font_size >= 24
        output.text_fits[key] = True
        halo = style.scaled(style.halo, int(img_w))
        number = str(item.payload.get("index", ""))
        cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
        radius = rect[2] / 2
        if style.badge_shape == "circle":
            output.fragments.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius + halo:.1f}" fill="#ffffff"/>'
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{colors["badge"]}"/>'
                f'<text x="{cx:.1f}" y="{cy + font_size * 0.35:.1f}" text-anchor="middle" '
                f'font-family="{_escape(style.family)}" font-weight="600" font-size="{font_size:.1f}" '
                f'fill="{colors["badgeText"]}">{_escape(number)}</text>')
        else:
            output.fragments.append(
                f'<rect x="{rect[0] - halo:.1f}" y="{rect[1] - halo:.1f}" '
                f'width="{rect[2] + 2 * halo:.1f}" height="{rect[3] + 2 * halo:.1f}" rx="{size / 4:.1f}" fill="#ffffff"/>'
                f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
                f'rx="{size / 4:.1f}" fill="{colors["badge"]}"/>'
                f'<text x="{cx:.1f}" y="{cy + font_size * 0.35:.1f}" text-anchor="middle" '
                f'font-family="{_escape(style.family)}" font-weight="600" font-size="{font_size:.1f}" '
                f'fill="{colors["badgeText"]}">{_escape(number)}</text>')
        return
    if item.type in ("callout", "label"):
        max_chars = 280 if item.type == "callout" else 140
        _ = max_chars
        font_size = style.scaled(style.callout_font_size, int(img_w))
        font_path = regular_font_path(style)
        padding = style.scaled(style.callout_padding, int(img_w))
        max_width = min(style.scaled(style.callout_max_width, int(img_w)), img_w - 2 * padding)
        lines = wrap_text(item.payload["text"], font_path, font_size, max_width - 2 * padding)
        longest = max((measure_text_width(line, font_path, font_size) for line in lines), default=0.0)
        box_w = longest + 2 * padding
        line_h = font_size * 1.3
        box_h = line_h * len(lines) + 2 * padding
        output.text_fits[key] = box_w <= max_width + 1.0
        output.text_colors[key] = colors["calloutText"]
        output.text_sizes[key] = font_size
        output.is_large_text[key] = font_size >= 24
        if item.placement.mode == "force" and item.placement.side:
            anchor_corner = {"top": (target[0], target[1] - box_h - GAP_PX),
                             "bottom": (target[0], target[1] + target[3] + GAP_PX),
                             "left": (target[0] - box_w - GAP_PX, target[1]),
                             "right": (target[0] + target[2] + GAP_PX, target[1])}[item.placement.side]
            rect, side = apply_forced((*anchor_corner, box_w, box_h), item.placement.side,
                                      item.placement.dx, item.placement.dy, img_w, img_h)
        elif item.type == "label":
            rect, side = _clamp_inside((target[0], target[1] - box_h - GAP_PX, box_w, box_h),
                                       img_w, img_h), "top"
        else:
            rect, side = callout_position(target, box_w, box_h, img_w, img_h)
        output.marks[key] = rect
        output.sides[key] = side
        halo = style.scaled(style.halo, int(img_w))
        texts = "".join(
            f'<text x="{rect[0] + padding:.1f}" y="{rect[1] + padding + font_size + n * line_h:.1f}" '
            f'font-family="{_escape(style.family)}" font-weight="400" font-size="{font_size:.1f}" '
            f'fill="{colors["calloutText"]}">{_escape(line)}</text>'
            for n, line in enumerate(lines))
        leader = ""
        if item.type == "callout":
            x0, y0 = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
            x1, y1 = target[0] + target[2] / 2, target[1] + target[3] / 2
            leader = (f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
                      f'stroke="{colors["arrow"]}" stroke-width="{style.scaled(style.callout_leader_width, int(img_w)):.1f}"/>')
        output.fragments.append(
            f'<rect x="{rect[0] - halo:.1f}" y="{rect[1] - halo:.1f}" width="{rect[2] + 2 * halo:.1f}" '
            f'height="{rect[3] + 2 * halo:.1f}" rx="6" fill="#ffffff"/>'
            f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
            f'rx="6" fill="{colors["callout"]}"/>') 
        output.fragments.append(leader + texts)
        return
    if item.type == "highlight":
        stroke = style.scaled(style.stroke, int(img_w))
        shape = item.payload.get("shape", "box")
        rect = (target[0] - stroke, target[1] - stroke, target[2] + 2 * stroke, target[3] + 2 * stroke)
        output.marks[key] = rect
        output.sides[key] = "target"
        if shape == "box":
            output.fragments.append(
                f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
                f'fill="{colors["highlight"]}" fill-opacity="0.18" stroke="{colors["highlight"]}" '
                f'stroke-width="{stroke:.1f}"/>')
        elif shape == "rounded":
            output.fragments.append(
                f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
                f'rx="{stroke * 3:.1f}" fill="none" stroke="{colors["highlight"]}" stroke-width="{stroke:.1f}"/>')
        else:
            output.fragments.append(
                f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
                f'fill="none" stroke="{colors["highlight"]}" stroke-width="{stroke:.1f}"/>')
        return
    if item.type == "arrow":
        output.marks[key] = target
        output.sides[key] = "target"
        second = arrow_box(item, arrow_target, resolved)
        x0, y0 = target[0] + target[2] / 2, target[1] + target[3] / 2
        x1, y1 = second[0] + second[2] / 2, second[1] + second[3] / 2
        width = style.scaled(style.arrow_width, int(img_w))
        head = style.scaled(style.arrow_head, int(img_w))
        output.fragments.append(
            f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
            f'stroke="{colors["arrow"]}" stroke-width="{width:.1f}"/>'
            f'<circle cx="{x1:.1f}" cy="{y1:.1f}" r="{head / 2:.1f}" fill="{colors["arrow"]}"/>')
        if item.payload.get("text"):
            font_size = style.scaled(style.callout_font_size, int(img_w))
            output.text_colors[key] = colors["calloutText"]
            output.text_sizes[key] = font_size
            output.is_large_text[key] = font_size >= 24
            output.text_fits[key] = True
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            output.fragments.append(
                f'<text x="{mx:.1f}" y="{my:.1f}" text-anchor="middle" '
                f'font-family="{_escape(style.family)}" font-weight="400" font-size="{font_size:.1f}" '
                f'fill="{colors["calloutText"]}" stroke="#ffffff" stroke-width="3" '
                f'paint-order="stroke">{_escape(item.payload["text"])}</text>')
        return
    if item.type == "spotlight":
        dim = float(item.payload.get("dim", style.spotlight_dim))
        x, y, w, h = target
        output.marks[key] = target
        output.sides[key] = "target"
        fill = colors["spotlight"]
        output.fragments.append(
            f'<rect x="0" y="0" width="{img_w:.1f}" height="{y:.1f}" fill="{fill}" fill-opacity="{dim}"/>'
            f'<rect x="0" y="{y + h:.1f}" width="{img_w:.1f}" height="{img_h - (y + h):.1f}" '
            f'fill="{fill}" fill-opacity="{dim}"/>'
            f'<rect x="0" y="{y:.1f}" width="{x:.1f}" height="{h:.1f}" fill="{fill}" fill-opacity="{dim}"/>'
            f'<rect x="{x + w:.1f}" y="{y:.1f}" width="{img_w - (x + w):.1f}" height="{h:.1f}" '
            f'fill="{fill}" fill-opacity="{dim}"/>')
        return
    if item.type == "blur":
        output.marks[key] = target
        output.sides[key] = "target"
        output.blur_boxes[key] = target
        radius = item.payload.get("radius")
        output.blur_radius[key] = int(radius) if radius is not None else 12
        return
    if item.type == "redact":
        output.marks[key] = target
        output.sides[key] = "target"
        output.redact_boxes[key] = target
        return
    if item.type in ("icon", "image"):
        name = item.payload.get("name", "")
        width = float(item.payload.get("width", style.scaled(style.badge_size * 2, int(img_w))))
        height = width
        data = assets.get(name)
        rect, side = badge_box_position(target, width, height, img_w, img_h)
        if item.placement.mode == "force" and item.placement.side:
            rect, side = apply_forced(rect, item.placement.side, item.placement.dx, item.placement.dy, img_w, img_h)
        output.marks[key] = rect
        output.sides[key] = side
        if data is None:
            output.fragments.append(
                f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
                f'fill="{colors["callout"]}" stroke="{colors["arrow"]}" stroke-width="2"/>'
                f'<text x="{rect[0] + rect[2] / 2:.1f}" y="{rect[1] + rect[3] / 2:.1f}" text-anchor="middle" '
                f'font-family="{_escape(style.family)}" font-size="12" '
                f'fill="{colors["calloutText"]}">{_escape(name)}</text>')
        else:
            raw, mime = data
            embedded = _embed_asset(raw, mime, int(rect[2]))
            output.fragments.append(
                f'<image x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
                f'href="data:{mime};base64,{embedded}"/>')
        return


def arrow_box(item: Item, target: Box | None, resolved: ResolvedElement) -> tuple[float, float, float, float]:
    if target is not None:
        return (target.x, target.y, target.w, target.h)
    to = item.payload.get("to") or {}
    if isinstance(to, dict) and to.get("kind") == "coords":
        return (float(to.get("x", 0)), float(to.get("y", 0)),
                float(to.get("width", 0)), float(to.get("height", 0)))
    box = resolved.box
    return (box.x + box.w + GAP_PX * 4, box.y, box.w, box.h)


def _embed_asset(raw: bytes, mime: str, width: int) -> str:
    if mime == "image/svg+xml":
        try:
            import resvg_py

            png = resvg_py.svg_to_bytes(svg_string=raw.decode("utf-8"), width=max(1, width),
                                        font_files=bundled_font_files(), skip_system_fonts=True)
            return base64.b64encode(png).decode("ascii")
        except Exception:
            return base64.b64encode(raw).decode("ascii")
    return base64.b64encode(raw).decode("ascii")


def build_overlay_svg(spec: Spec, style: Style, plan_result: RenderPlan, image_w: int, image_h: int) -> str:
    body = "".join(plan_result.fragments)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{image_w}" height="{image_h}" '
            f'viewBox="0 0 {image_w} {image_h}">{body}</svg>')


def rasterize_overlay(svg: str, image_w: int, image_h: int) -> bytes:
    try:
        import resvg_py
    except ImportError as error:
        raise RuntimeError("resvg-py is required to render annotations (pip install resvg-py).") from error
    return resvg_py.svg_to_bytes(svg_string=svg, width=image_w, height=image_h,
                                 font_files=bundled_font_files(), skip_system_fonts=True)


def sample_under_color(clean_png: bytes, rect: tuple[float, float, float, float]) -> tuple[int, int, int]:
    from PIL import Image

    image = Image.open(io.BytesIO(clean_png)).convert("RGB")
    x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(image.width, x + max(1, w)), min(image.height, y + max(1, h))
    if x1 <= x0 or y1 <= y0:
        return (255, 255, 255)
    crop = image.crop((x0, y0, x1, y1)).resize((8, 8))
    from PIL.ImageStat import Stat

    mean = Stat(crop).mean
    return (int(mean[0]), int(mean[1]), int(mean[2]))


def render_annotated_image(clean_png: bytes, spec: Spec, measured: dict[str, ResolvedElement],
                           style: Style, *,
                           assets: dict[str, tuple[bytes, str]] | None = None,
                           arrow_targets: dict[str, Box] | None = None) -> RenderResult:
    from PIL import Image, ImageFilter

    clean = Image.open(io.BytesIO(clean_png)).convert("RGBA")
    image_w, image_h = clean.size
    output = plan(spec, measured, style, image_w, image_h, assets=assets, arrow_targets=arrow_targets)
    svg = build_overlay_svg(spec, style, output, image_w, image_h)
    overlay_png = rasterize_overlay(svg, image_w, image_h)
    overlay = Image.open(io.BytesIO(overlay_png)).convert("RGBA")
    composed = Image.alpha_composite(clean, overlay)
    flat = composed.convert("RGB")
    for key, rect in output.blur_boxes.items():
        x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        region = flat.crop((max(0, x), max(0, y), min(image_w, x + max(1, w)), min(image_h, y + max(1, h))))
        blurred = region.filter(ImageFilter.GaussianBlur(radius=output.blur_radius.get(key, 12)))
        flat.paste(blurred, (max(0, x), max(0, y)))
    for key, rect in output.redact_boxes.items():
        from PIL import ImageDraw

        drawer = ImageDraw.Draw(flat)
        x, y, w, h = rect
        drawer.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
    buffer = io.BytesIO()
    flat.save(buffer, format="PNG")
    return RenderResult(png=buffer.getvalue(), svg=svg, marks=output.marks, sides=output.sides,
                        text_colors=output.text_colors, text_sizes=output.text_sizes,
                        text_fits=output.text_fits, is_large_text=output.is_large_text)
