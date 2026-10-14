from __future__ import annotations

import base64
import importlib
import importlib.metadata
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.sax import saxutils

from eks_harness.annotate.measure import Box as LegacyBox
from eks_harness.annotate.measure import ResolvedElement
from eks_harness.annotate.render import apply_forced, badge_box_position, plan
from eks_harness.annotate.spec import Anchor, Item, Spec, Style
from eks_harness.annotate.spec import Placement as LegacyPlacement
from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.ops.overlay import (
    Arrow,
    Blur,
    Callout,
    Caption,
    Highlight,
    Icon,
    Image,
    OverlayBase,
    Placement,
    Redact,
    Spotlight,
    Step,
    Title,
)
from eks_harness.ir.presets import OverlayStyle
from eks_harness.ir.registry import Registry, RegistryError, default_registry
from eks_harness.media.fonts import shipped_font
from eks_harness.media.overlays.boxmap import Box
from eks_harness.media.overlays.icons import icon_svg

Renderer = Literal["screenshot", "video"]

DEFAULT_STYLE = "kb"
DEFAULT_BLUR_RADIUS = 8
DRAW_VERSION = "1"
REGULAR, SEMIBOLD = 400, 600
HALO = "#ffffff"
ICON_SCALE = 1.25
IMAGE_SUFFIXES_SVG = frozenset({".svg"})

_resvg: Any = importlib.import_module("resvg_py")


@dataclass(frozen=True, slots=True)
class Drawing:
    fragments: tuple[str, ...] = ()
    blurs: tuple[tuple[Box, int], ...] = ()
    marks: dict[str, Box] = field(default_factory=dict[str, Box])


@dataclass(frozen=True, slots=True)
class ResolvedStyle:
    name: str
    model: OverlayStyle
    legacy: Style
    fonts: tuple[Path, ...]


def _escape(text: str) -> str:
    return saxutils.escape(text, {'"': "&quot;"})


def font_files(model: OverlayStyle) -> tuple[Path, ...]:
    family = model.fonts.family
    found: list[Path] = []
    for weight, file in ((REGULAR, model.fonts.regular), (SEMIBOLD, model.fonts.semibold)):
        path = shipped_font(family, weight)
        if path.name != file:
            raise NodeFailure("font_unavailable",
                              f"the overlay style names {file} for {family} weight {weight}, but that weight ships as "
                              f"{path.name}",
                              detail={"family": family, "weight": weight, "file": file})
        if not path.is_file():
            raise NodeFailure("font_unavailable", f"the shipped font file {path} is missing from this install",
                              detail={"family": family, "weight": weight})
        found.append(path)
    return tuple(found)


def legacy_style(name: str, model: OverlayStyle) -> Style:
    colors = model.colors
    return Style(
        name=name, ref_width=model.ref_width, family=model.fonts.family, regular=model.fonts.regular,
        semibold=model.fonts.semibold,
        colors={"badge": colors.badge, "badgeText": colors.badge_text, "callout": colors.callout,
                "calloutText": colors.callout_text, "highlight": colors.highlight, "arrow": colors.arrow,
                "spotlight": colors.spotlight, "captionBar": colors.caption_bar, "captionText": colors.caption_text,
                "titleCard": colors.title_card, "titleText": colors.title_text},
        stroke=model.stroke, halo=model.halo, badge_shape=model.badge_shape, badge_size=model.badge_size,
        badge_font_size=model.badge_font_size, callout_font_size=model.callout_font_size,
        callout_padding=model.callout_padding, callout_leader_width=model.callout_leader_width,
        callout_max_width=model.callout_max_width, arrow_width=model.arrow_width, arrow_head=model.arrow_head,
        spotlight_dim=model.spotlight_dim, caption_font_size=model.caption_font_size,
        caption_bar_height=model.caption_bar_height, title_font_size=model.title_font_size,
        title_sub_font_size=model.title_sub_font_size)


def style_preset(name: str, registry: Registry | None = None) -> OverlayStyle:
    found = registry if registry is not None else default_registry()
    try:
        value = found.preset("style", name).value
    except RegistryError as error:
        raise NodeFailure("args_invalid", f"overlay style {name!r} is not a registered style preset",
                          detail={"style": name}) from error
    if not isinstance(value, OverlayStyle):
        raise NodeFailure("args_invalid", f"style preset {name!r} is not an overlay style", detail={"style": name})
    return value


def resolve_style(name: str | None, model: OverlayStyle | None = None, registry: Registry | None = None
                  ) -> ResolvedStyle:
    chosen = name or DEFAULT_STYLE
    value = model if model is not None else style_preset(chosen, registry)
    return ResolvedStyle(name=chosen, model=value, legacy=legacy_style(chosen, value), fonts=font_files(value))


def _placement(placement: Placement | None) -> LegacyPlacement:
    if placement is None or placement.side is None:
        return LegacyPlacement()
    return LegacyPlacement(mode="force", side=placement.side, dx=placement.dx, dy=placement.dy)


def _coords(box: Box) -> Anchor:
    x, y, w, h = box
    return Anchor(kind="coords", x=x, y=y, width=max(w, 1.0), height=max(h, 1.0))


def _element(box: Box) -> ResolvedElement:
    x, y, w, h = box
    return ResolvedElement(box=LegacyBox(x=x, y=y, w=w, h=h))


def _legacy_fragments(items: Sequence[tuple[Item, Box | None, Box | None]], style: Style,
                      size: tuple[int, int]) -> tuple[list[str], dict[str, Box]]:
    measured = {item.item_id or "": _element(box) for item, box, _ in items if box is not None}
    arrows = {item.item_id or "": LegacyBox(x=to[0], y=to[1], w=to[2], h=to[3]) for item, _, to in items
              if to is not None}
    spec = Spec(version=1, style=style.name, items=tuple(item for item, _, _ in items))
    output = plan(spec, measured, style, size[0], size[1], arrow_targets=arrows)
    return list(output.fragments), dict(output.marks)


def _item(kind: str, key: str, anchor: Box | None, placement: Placement | None, payload: dict[str, Any]) -> Item:
    return Item(type=kind, anchor=_coords(anchor) if anchor is not None else None, item_id=key,
                placement=_placement(placement), label=None, payload=payload)


def _need(anchor: Box | None, overlay: OverlayBase) -> Box:
    if anchor is None:
        raise NodeFailure("target_not_found", f"the {overlay.kind} overlay has no resolved anchor box")
    return anchor


def _image_png(path: Path, width: int | None) -> tuple[bytes, int, int]:
    from PIL import Image as PILImage

    if path.suffix.lower() in IMAGE_SUFFIXES_SVG:
        svg = path.read_text(encoding="utf-8")
        png = bytes(_resvg.svg_to_bytes(svg_string=svg, width=width, skip_system_fonts=True))
        with PILImage.open(io.BytesIO(png)) as rendered:
            return png, int(rendered.width), int(rendered.height)
    try:
        with PILImage.open(path) as opened:
            converted = opened.convert("RGBA")
    except OSError as error:
        raise NodeFailure("content_unavailable", f"the overlay image {path.name} is not a readable image: {error}"
                          ) from error
    buffer = io.BytesIO()
    converted.save(buffer, format="PNG")
    return buffer.getvalue(), int(converted.width), int(converted.height)


def _badge_rect(anchor: Box, box_w: float, box_h: float, placement: Placement | None,
                size: tuple[int, int]) -> Box:
    rect, side = badge_box_position(anchor, box_w, box_h, float(size[0]), float(size[1]))
    if placement is not None and placement.side is not None:
        rect, side = apply_forced(rect, placement.side, placement.dx, placement.dy, float(size[0]), float(size[1]))
    _ = side
    return rect


def _icon(overlay: Icon, anchor: Box, style: Style, size: tuple[int, int]) -> tuple[list[str], Box]:
    diameter = style.scaled(style.badge_size, size[0]) * ICON_SCALE
    rect = _badge_rect(anchor, diameter, diameter, overlay.placement, size)
    cx, cy, radius = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2, rect[2] / 2
    halo = style.scaled(style.halo, size[0])
    glyph = radius * 1.2
    return [f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius + halo:.1f}" fill="{HALO}"/>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{style.colors["badge"]}"/>'
            + icon_svg(overlay.name, x=cx - glyph / 2, y=cy - glyph / 2, size=glyph,
                       color=style.colors["badgeText"])], rect


def _image(overlay: Image, anchor: Box, image: Path | None, size: tuple[int, int]) -> tuple[list[str], Box]:
    if image is None:
        raise NodeFailure("content_unavailable", "the image overlay has no resolved image file")
    png, width, height = _image_png(image, overlay.width)
    shown_w = float(overlay.width or width)
    shown_h = shown_w * height / width if width else shown_w
    rect = _badge_rect(anchor, shown_w, shown_h, overlay.placement, size)
    data = base64.b64encode(png).decode("ascii")
    fragment = (f'<image x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2]:.1f}" height="{rect[3]:.1f}" '
                f'preserveAspectRatio="none" href="data:image/png;base64,{data}"/>')
    return [fragment], rect


def _caption_step(step: int, bar: Box, style: Style, size: tuple[int, int]) -> str:
    radius = min(style.scaled(style.badge_size, size[0]), bar[3] * 0.8) / 2
    padding = style.scaled(style.callout_padding, size[0])
    cx, cy = bar[0] + padding + radius, bar[1] + bar[3] / 2
    font = style.scaled(style.badge_font_size, size[0])
    return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{style.colors["badge"]}"/>'
            f'<text x="{cx:.1f}" y="{cy + font * 0.35:.1f}" text-anchor="middle" '
            f'font-family="{_escape(style.family)}" font-weight="600" font-size="{font:.1f}" '
            f'fill="{style.colors["badgeText"]}">{step}</text>')


def _kicker(text: str, style: Style, size: tuple[int, int]) -> str:
    title = style.scaled(style.title_font_size, size[0])
    font = style.scaled(style.title_sub_font_size, size[0])
    y = size[1] / 2 - title * 1.15
    return (f'<text x="{size[0] / 2}" y="{y:.1f}" text-anchor="middle" font-family="{_escape(style.family)}" '
            f'font-weight="600" font-size="{font:.1f}" fill="{style.colors["titleText"]}" '
            f'fill-opacity="0.8">{_escape(text)}</text>')


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise NodeFailure("args_invalid", f"overlay text {value!r} was not resolved to a string")
    return value


def draw(overlay: OverlayBase, *, key: str, anchor: Box | None, to: Box | None, image: Path | None,
         style: ResolvedStyle, size: tuple[int, int], step: int | None, renderer: Renderer) -> Drawing:
    legacy = style.legacy
    match overlay:
        case Title():
            if renderer == "screenshot":
                raise NodeFailure("args_invalid", "title overlays do not render on screenshots; use a caption",
                                  detail={"kind": "title"})
            item = _item("title", key, None, None, {"text": _text(overlay.text),
                                                    "subtitle": _text(overlay.subtitle) if overlay.subtitle else ""})
            fragments, marks = _legacy_fragments([(item, None, None)], legacy, size)
            if overlay.kicker:
                fragments.append(_kicker(_text(overlay.kicker), legacy, size))
            return Drawing(tuple(fragments), marks=marks)
        case Caption():
            item = _item("caption", key, None, None, {"text": _text(overlay.text), "position": overlay.position})
            fragments, marks = _legacy_fragments([(item, None, None)], legacy, size)
            if overlay.step is not None and key in marks:
                fragments.append(_caption_step(overlay.step, marks[key], legacy, size))
            return Drawing(tuple(fragments), marks=marks)
        case Callout():
            item = _item("callout", key, _need(anchor, overlay), overlay.placement, {"text": _text(overlay.text)})
            fragments, marks = _legacy_fragments([(item, anchor, None)], legacy, size)
            return Drawing(tuple(fragments), marks=marks)
        case Spotlight():
            box = _need(anchor, overlay)
            payload: dict[str, Any] = {"dim": overlay.dim} if overlay.dim is not None else {}
            items = [(_item("spotlight", key, box, None, payload), box, None)]
            if overlay.text:
                items.append((_item("callout", f"{key}-text", box, overlay.placement, {"text": _text(overlay.text)}),
                              box, None))
            fragments, marks = _legacy_fragments(items, legacy, size)
            return Drawing(tuple(fragments), marks=marks)
        case Highlight():
            box = _need(anchor, overlay)
            fragments, marks = _legacy_fragments([(_item("highlight", key, box, None, {"shape": overlay.shape}),
                                                   box, None)], legacy, size)
            return Drawing(tuple(fragments), marks=marks)
        case Arrow():
            box = _need(anchor, overlay)
            if to is None:
                raise NodeFailure("target_not_found", "the arrow overlay has no resolved target box")
            payload = {"to": _coords(to).to_dict(), "text": _text(overlay.text) if overlay.text else ""}
            fragments, marks = _legacy_fragments([(_item("arrow", key, box, None, payload), box, to)], legacy, size)
            return Drawing(tuple(fragments), marks=marks)
        case Blur():
            box = _need(anchor, overlay)
            radius = overlay.radius if overlay.radius is not None else DEFAULT_BLUR_RADIUS
            return Drawing((), blurs=((box, radius),), marks={key: box})
        case Redact():
            x, y, w, h = _need(anchor, overlay)
            return Drawing((f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#000000"/>',),
                           marks={key: (x, y, w, h)})
        case Icon():
            fragments, rect = _icon(overlay, _need(anchor, overlay), legacy, size)
            return Drawing(tuple(fragments), marks={key: rect})
        case Image():
            fragments, rect = _image(overlay, _need(anchor, overlay), image, size)
            return Drawing(tuple(fragments), marks={key: rect})
        case Step():
            box = _need(anchor, overlay)
            index = overlay.index if overlay.index is not None else step if step is not None else 1
            fragments, marks = _legacy_fragments([(_item("step", key, box, overlay.placement, {"index": index}),
                                                   box, None)], legacy, size)
            return Drawing(tuple(fragments), marks=marks)
        case _:
            raise NodeFailure("args_invalid", f"the overlay kind {overlay.kind!r} has no built-in {renderer} renderer",
                              detail={"kind": overlay.kind})


def svg_document(fragments: Sequence[str], size: tuple[int, int]) -> str:
    width, height = size
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">{"".join(fragments)}</svg>')


def rasterize(svg: str, size: tuple[int, int], fonts: Sequence[Path]) -> bytes:
    return bytes(_resvg.svg_to_bytes(svg_string=svg, width=size[0], height=size[1],
                                     font_files=[str(path) for path in fonts], skip_system_fonts=True))


def versions() -> dict[str, str]:
    found = {"draw": DRAW_VERSION}
    for package in ("resvg-py", "pillow"):
        try:
            found[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            found[package] = "unknown"
    return found


__all__ = [
    "DEFAULT_BLUR_RADIUS",
    "DEFAULT_STYLE",
    "Drawing",
    "Renderer",
    "ResolvedStyle",
    "draw",
    "font_files",
    "legacy_style",
    "rasterize",
    "resolve_style",
    "style_preset",
    "svg_document",
    "versions",
]
