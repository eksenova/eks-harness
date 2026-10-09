from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from eks_harness.ids import slugify

SPEC_VERSION = 1
MAX_ITEMS = 100

ITEM_TYPES = frozenset({
    "step", "callout", "label", "highlight", "arrow", "spotlight",
    "blur", "redact", "icon", "image", "caption", "title",
})

ANCHOR_KINDS = frozenset({"selector", "role", "text", "testId", "coords"})

PLACEMENT_SIDES = frozenset({"top", "bottom", "left", "right"})

EMPHASIS = frozenset({"none", "freeze", "slow"})

HIGHLIGHT_SHAPES = frozenset({"box", "outline", "rounded"})

BADGE_SHAPES = frozenset({"circle", "rounded"})

CAPTION_POSITIONS = frozenset({"top", "bottom"})

TYPES_WITHOUT_ANCHOR = frozenset({"caption", "title"})

TOP_LEVEL_FIELDS = frozenset({"version", "style", "items"})

COMMON_ITEM_FIELDS = frozenset({"type", "anchor", "id", "placement", "label"})

TYPE_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "step": frozenset({"index", "emphasis"}),
    "callout": frozenset({"text", "emphasis"}),
    "label": frozenset({"text", "emphasis"}),
    "highlight": frozenset({"shape", "emphasis"}),
    "arrow": frozenset({"to", "text", "emphasis"}),
    "spotlight": frozenset({"dim", "emphasis"}),
    "blur": frozenset({"radius", "emphasis"}),
    "redact": frozenset({"emphasis"}),
    "icon": frozenset({"name", "emphasis"}),
    "image": frozenset({"name", "width", "emphasis"}),
    "caption": frozenset({"text", "position", "start", "end", "emphasis"}),
    "title": frozenset({"text", "subtitle", "duration", "emphasis"}),
}

ANCHOR_FIELDS: dict[str, frozenset[str]] = {
    "selector": frozenset({"kind", "selector"}),
    "role": frozenset({"kind", "role", "name"}),
    "text": frozenset({"kind", "text"}),
    "testId": frozenset({"kind", "testId"}),
    "coords": frozenset({"kind", "x", "y", "width", "height"}),
}

STYLE_COLOR_KEYS = (
    "badge", "badgeText", "callout", "calloutText", "highlight", "arrow",
    "spotlight", "captionBar", "captionText", "titleCard", "titleText",
)

ASSET_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".svg"})
ASSET_NAME_MAX = 64


class SpecError(ValueError):
    pass


def _require_mapping(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise SpecError(f"{what} must be an object.")
    return value


def _require_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{what} must be a number.")
    return float(value)


def _require_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{what} must be an integer.")
    return value


def _require_text(value: Any, what: str, *, min_len: int = 1, max_len: int | None = None) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{what} must be a string.")
    if len(value) < min_len:
        raise SpecError(f"{what} must not be empty.")
    if max_len is not None and len(value) > max_len:
        raise SpecError(f"{what} must be at most {max_len} chars.")
    return value


def _check_color(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{what} must be a #rrggbb string.")
    text = value.strip().lower()
    if len(text) != 7 or not text.startswith("#"):
        raise SpecError(f"{what} must be a #rrggbb string.")
    try:
        int(text[1:], 16)
    except ValueError:
        raise SpecError(f"{what} must be a #rrggbb string.") from None
    return text


@dataclass(frozen=True)
class Anchor:
    kind: str
    selector: str | None = None
    role: str | None = None
    name: str | None = None
    text: str | None = None
    test_id: str | None = None
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None

    def is_coords(self) -> bool:
        return self.kind == "coords"

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "selector":
            return {"kind": "selector", "selector": self.selector}
        if self.kind == "role":
            data: dict[str, Any] = {"kind": "role", "role": self.role}
            if self.name is not None:
                data["name"] = self.name
            return data
        if self.kind == "text":
            return {"kind": "text", "text": self.text}
        if self.kind == "testId":
            return {"kind": "testId", "testId": self.test_id}
        return {"kind": "coords", "x": self.x, "y": self.y,
                "width": self.width, "height": self.height}


@dataclass(frozen=True)
class Placement:
    mode: str = "auto"
    side: str | None = None
    dx: float = 0.0
    dy: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        if self.mode == "auto":
            return {"mode": "auto"}
        return {"mode": "force", "side": self.side, "dx": self.dx, "dy": self.dy}


@dataclass(frozen=True)
class Item:
    type: str
    anchor: Anchor | None
    item_id: str | None
    placement: Placement
    label: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    emphasis: str = "none"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.anchor is not None:
            data["anchor"] = self.anchor.to_dict()
        if self.item_id is not None:
            data["id"] = self.item_id
        if self.placement.mode != "auto":
            data["placement"] = self.placement.to_dict()
        if self.label is not None:
            data["label"] = self.label
        data.update(self.payload)
        if self.emphasis != "none":
            data["emphasis"] = self.emphasis
        return data


@dataclass(frozen=True)
class Spec:
    version: int
    style: str | None
    items: tuple[Item, ...]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"version": self.version, "items": [i.to_dict() for i in self.items]}
        if self.style is not None:
            data["style"] = self.style
        return data


def parse_anchor(raw: Any, *, item_type: str, viewport: str = "desktop") -> Anchor:
    data = _require_mapping(raw, "anchor")
    kind = data.get("kind")
    if kind not in ANCHOR_KINDS:
        raise SpecError(f"anchor kind must be one of {sorted(ANCHOR_KINDS)}.")
    allowed = ANCHOR_FIELDS[kind]
    unknown = set(data) - allowed
    if unknown:
        raise SpecError(f"unknown anchor fields for {kind}: {sorted(unknown)}.")
    if kind == "selector":
        if viewport == "mobile":
            raise SpecError("selector anchors are rejected on mobile (use testId or text).")
        return Anchor(kind="selector", selector=_require_text(data.get("selector"), "anchor.selector"))
    if kind == "role":
        return Anchor(
            kind="role",
            role=_require_text(data.get("role"), "anchor.role"),
            name=data.get("name") if data.get("name") is None else _require_text(data.get("name"), "anchor.name"),
        )
    if kind == "text":
        return Anchor(kind="text", text=_require_text(data.get("text"), "anchor.text"))
    if kind == "testId":
        return Anchor(kind="testId", test_id=_require_text(data.get("testId"), "anchor.testId"))
    x = _require_number(data.get("x"), "anchor.x")
    y = _require_number(data.get("y"), "anchor.y")
    width = _require_number(data.get("width"), "anchor.width")
    height = _require_number(data.get("height"), "anchor.height")
    if width <= 0 or height <= 0:
        raise SpecError("coords anchor width and height must be positive.")
    return Anchor(kind="coords", x=x, y=y, width=width, height=height)


def parse_placement(raw: Any) -> Placement:
    if raw is None:
        return Placement()
    data = _require_mapping(raw, "placement")
    mode = data.get("mode", "auto")
    if mode == "auto":
        unknown = set(data) - {"mode"}
        if unknown:
            raise SpecError(f"unknown placement fields: {sorted(unknown)}.")
        return Placement()
    if mode != "force":
        raise SpecError("placement mode must be auto or force.")
    unknown = set(data) - {"mode", "side", "dx", "dy"}
    if unknown:
        raise SpecError(f"unknown placement fields: {sorted(unknown)}.")
    side = data.get("side")
    if side not in PLACEMENT_SIDES:
        raise SpecError(f"placement side must be one of {sorted(PLACEMENT_SIDES)}.")
    dx = _require_number(data.get("dx", 0), "placement.dx")
    dy = _require_number(data.get("dy", 0), "placement.dy")
    return Placement(mode="force", side=side, dx=dx, dy=dy)


def _parse_payload(item_type: str, data: dict, *, position: int, for_video: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if item_type == "step":
        if "index" in data:
            payload["index"] = _require_int(data["index"], "step.index")
        else:
            payload["index"] = position
        return payload
    if item_type == "callout":
        payload["text"] = _require_text(data.get("text"), "callout.text", max_len=280)
        return payload
    if item_type == "label":
        payload["text"] = _require_text(data.get("text"), "label.text", max_len=140)
        return payload
    if item_type == "highlight":
        shape = data.get("shape", "box")
        if shape not in HIGHLIGHT_SHAPES:
            raise SpecError(f"highlight shape must be one of {sorted(HIGHLIGHT_SHAPES)}.")
        payload["shape"] = shape
        return payload
    if item_type == "arrow":
        if "to" not in data:
            raise SpecError("arrow requires a to anchor.")
        payload["to"] = data["to"]
        text = data.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise SpecError("arrow text must be a string.")
        if len(text) > 140:
            raise SpecError("arrow text must be at most 140 chars.")
        payload["text"] = text
        return payload
    if item_type == "spotlight":
        if "dim" in data:
            dim = _require_number(data["dim"], "spotlight.dim")
            if not 0.0 <= dim <= 1.0:
                raise SpecError("spotlight dim must be between 0.0 and 1.0.")
            payload["dim"] = dim
        return payload
    if item_type == "blur":
        if "radius" in data:
            payload["radius"] = _require_int(data["radius"], "blur.radius")
            if payload["radius"] < 0:
                raise SpecError("blur radius must not be negative.")
        return payload
    if item_type == "redact":
        return payload
    if item_type == "icon":
        payload["name"] = _require_text(data.get("name"), "icon.name", max_len=64)
        return payload
    if item_type == "image":
        payload["name"] = _require_text(data.get("name"), "image.name", max_len=64)
        if "width" in data:
            payload["width"] = _require_int(data["width"], "image.width")
            if payload["width"] <= 0:
                raise SpecError("image width must be positive.")
        return payload
    if item_type == "caption":
        payload["text"] = _require_text(data.get("text"), "caption.text", max_len=280)
        position_value = data.get("position", "bottom")
        if position_value not in CAPTION_POSITIONS:
            raise SpecError(f"caption position must be one of {sorted(CAPTION_POSITIONS)}.")
        payload["position"] = position_value
        if "start" in data or "end" in data:
            if not for_video:
                raise SpecError("caption start/end are video only; omit them on screenshots.")
            if "start" in data:
                payload["start"] = _require_number(data["start"], "caption.start")
            if "end" in data:
                payload["end"] = _require_number(data["end"], "caption.end")
            if "start" in payload and "end" in payload and payload["end"] <= payload["start"]:
                raise SpecError("caption end must be after start.")
        return payload
    if item_type == "title":
        if not for_video:
            raise SpecError("title items are video only on screenshots (use caption).")
        payload["text"] = _require_text(data.get("text"), "title.text", max_len=140)
        subtitle = data.get("subtitle", "")
        if subtitle is None:
            subtitle = ""
        if not isinstance(subtitle, str):
            raise SpecError("title subtitle must be a string.")
        if len(subtitle) > 200:
            raise SpecError("title subtitle must be at most 200 chars.")
        payload["subtitle"] = subtitle
        if "duration" in data:
            payload["duration"] = _require_number(data["duration"], "title.duration")
            if payload["duration"] <= 0:
                raise SpecError("title duration must be positive.")
        return payload
    raise SpecError(f"unknown item type {item_type!r}.")  # pragma: no cover


def parse_item(raw: Any, *, position: int, viewport: str = "desktop", for_video: bool = False) -> Item:
    data = _require_mapping(raw, f"items[{position}]")
    item_type = data.get("type")
    if item_type not in ITEM_TYPES:
        raise SpecError(f"items[{position}].type must be one of {sorted(ITEM_TYPES)}.")
    allowed = COMMON_ITEM_FIELDS | TYPE_PAYLOAD_FIELDS[item_type]
    unknown = set(data) - allowed
    if unknown:
        raise SpecError(f"items[{position}] has unknown fields: {sorted(unknown)}.")
    anchor: Anchor | None = None
    if item_type not in TYPES_WITHOUT_ANCHOR:
        if "anchor" not in data:
            raise SpecError(f"items[{position}] of type {item_type} requires an anchor.")
        anchor = parse_anchor(data["anchor"], item_type=item_type, viewport=viewport)
    elif "anchor" in data:
        raise SpecError(f"items[{position}] of type {item_type} must not carry an anchor.")
    item_id = data.get("id")
    if item_id is not None and not isinstance(item_id, str):
        raise SpecError(f"items[{position}].id must be a string.")
    if isinstance(item_id, str) and not item_id:
        raise SpecError(f"items[{position}].id must not be empty.")
    label = data.get("label")
    if label is not None and not isinstance(label, str):
        raise SpecError(f"items[{position}].label must be a string.")
    emphasis = data.get("emphasis", "none")
    if emphasis not in EMPHASIS:
        raise SpecError(f"items[{position}].emphasis must be one of {sorted(EMPHASIS)}.")
    payload = _parse_payload(item_type, data, position=position, for_video=for_video)
    if item_type == "arrow":
        payload["to"] = parse_anchor(payload["to"], item_type=item_type, viewport=viewport).to_dict()
    return Item(type=item_type, anchor=anchor, item_id=item_id,
                placement=parse_placement(data.get("placement")),
                label=label, payload=payload, emphasis=emphasis)


def parse_spec(raw: Any, *, viewport: str = "desktop", for_video: bool = False) -> Spec:
    data = _require_mapping(raw, "spec")
    unknown = set(data) - TOP_LEVEL_FIELDS
    if unknown:
        raise SpecError(f"unknown spec fields: {sorted(unknown)}.")
    if data.get("version") != SPEC_VERSION:
        raise SpecError("spec version must be 1.")
    style = data.get("style")
    if style is not None and not isinstance(style, str):
        raise SpecError("spec style must be a string.")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise SpecError("spec items must be a non-empty list.")
    if len(items) > MAX_ITEMS:
        raise SpecError(f"spec items must hold at most {MAX_ITEMS} entries.")
    parsed = tuple(parse_item(entry, position=n + 1, viewport=viewport, for_video=for_video)
                   for n, entry in enumerate(items))
    return Spec(version=1, style=style, items=parsed)


def loads_spec(text: str | bytes, *, viewport: str = "desktop", for_video: bool = False) -> Spec:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    stripped = text.strip()
    if not stripped:
        raise SpecError("spec must not be empty.")
    if stripped[0] in "{[":
        import json

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise SpecError(f"spec is not valid JSON: {error}") from None
    else:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SpecError(f"spec is not valid YAML: {error}") from None
    return parse_spec(raw, viewport=viewport, for_video=for_video)


def load_spec_file(path: str | Path, *, viewport: str = "desktop", for_video: bool = False) -> tuple[Spec, str]:
    raw_text = Path(path).read_text(encoding="utf-8")
    return loads_spec(raw_text, viewport=viewport, for_video=for_video), raw_text


@dataclass(frozen=True)
class Style:
    name: str
    ref_width: int
    family: str
    regular: str
    semibold: str
    colors: dict[str, str]
    stroke: int
    halo: int
    badge_shape: str
    badge_size: int
    badge_font_size: int
    callout_font_size: int
    callout_padding: int
    callout_leader_width: int
    callout_max_width: int
    arrow_width: int
    arrow_head: int
    spotlight_dim: float
    caption_font_size: int
    caption_bar_height: int
    title_font_size: int
    title_sub_font_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "refWidth": self.ref_width,
            "fonts": {"family": self.family, "regular": self.regular, "semibold": self.semibold},
            "colors": dict(self.colors),
            "stroke": self.stroke,
            "halo": self.halo,
            "badge": {"shape": self.badge_shape, "size": self.badge_size, "fontSize": self.badge_font_size},
            "callout": {"fontSize": self.callout_font_size, "padding": self.callout_padding,
                        "leaderWidth": self.callout_leader_width, "maxWidth": self.callout_max_width},
            "arrow": {"width": self.arrow_width, "head": self.arrow_head},
            "spotlight": {"dim": self.spotlight_dim},
            "caption": {"fontSize": self.caption_font_size, "barHeight": self.caption_bar_height},
            "title": {"fontSize": self.title_font_size, "subFontSize": self.title_sub_font_size},
        }

    def scaled(self, size: int | float, actual_width: int) -> float:
        return float(size) * float(actual_width) / float(self.ref_width)


def parse_style(raw: Any) -> Style:
    data = _require_mapping(raw, "style")
    unknown = set(data) - {"name", "refWidth", "fonts", "colors", "stroke", "halo", "badge", "callout", "arrow",
                           "spotlight", "caption", "title"}
    if unknown:
        raise SpecError(f"unknown style fields: {sorted(unknown)}.")
    name = _require_text(data.get("name"), "style.name", max_len=64)
    ref_width = _require_int(data.get("refWidth"), "style.refWidth")
    if ref_width <= 0:
        raise SpecError("style refWidth must be positive.")
    fonts = _require_mapping(data.get("fonts"), "style.fonts")
    if set(fonts) != {"family", "regular", "semibold"}:
        raise SpecError("style.fonts must hold exactly family, regular and semibold.")
    family = _require_text(fonts.get("family"), "style.fonts.family")
    regular = _require_text(fonts.get("regular"), "style.fonts.regular")
    semibold = _require_text(fonts.get("semibold"), "style.fonts.semibold")
    colors_raw = _require_mapping(data.get("colors"), "style.colors")
    if set(colors_raw) != set(STYLE_COLOR_KEYS):
        raise SpecError(f"style.colors must hold exactly {sorted(STYLE_COLOR_KEYS)}.")
    colors = {key: _check_color(colors_raw[key], f"style.colors.{key}") for key in STYLE_COLOR_KEYS}
    stroke = _require_int(data.get("stroke"), "style.stroke")
    halo = _require_int(data.get("halo"), "style.halo")
    badge = _require_mapping(data.get("badge"), "style.badge")
    if set(badge) != {"shape", "size", "fontSize"}:
        raise SpecError("style.badge must hold exactly shape, size and fontSize.")
    if badge["shape"] not in BADGE_SHAPES:
        raise SpecError(f"style.badge.shape must be one of {sorted(BADGE_SHAPES)}.")
    callout = _require_mapping(data.get("callout"), "style.callout")
    if set(callout) != {"fontSize", "padding", "leaderWidth", "maxWidth"}:
        raise SpecError("style.callout must hold exactly fontSize, padding, leaderWidth and maxWidth.")
    arrow = _require_mapping(data.get("arrow"), "style.arrow")
    if set(arrow) != {"width", "head"}:
        raise SpecError("style.arrow must hold exactly width and head.")
    spotlight = _require_mapping(data.get("spotlight"), "style.spotlight")
    dim = _require_number(spotlight.get("dim"), "style.spotlight.dim")
    if not 0.0 <= dim <= 1.0:
        raise SpecError("style.spotlight.dim must be between 0.0 and 1.0.")
    caption = _require_mapping(data.get("caption"), "style.caption")
    if set(caption) != {"fontSize", "barHeight"}:
        raise SpecError("style.caption must hold exactly fontSize and barHeight.")
    title = _require_mapping(data.get("title"), "style.title")
    if set(title) != {"fontSize", "subFontSize"}:
        raise SpecError("style.title must hold exactly fontSize and subFontSize.")
    for key in ("size", "fontSize"):
        _require_int(badge[key], f"style.badge.{key}")
    for key in ("fontSize", "padding", "leaderWidth", "maxWidth"):
        _require_int(callout[key], f"style.callout.{key}")
    for key in ("width", "head"):
        _require_int(arrow[key], f"style.arrow.{key}")
    for key in ("fontSize", "barHeight"):
        _require_int(caption[key], f"style.caption.{key}")
    for key in ("fontSize", "subFontSize"):
        _require_int(title[key], f"style.title.{key}")
    return Style(
        name=name, ref_width=ref_width, family=family, regular=regular, semibold=semibold,
        colors=colors, stroke=stroke, halo=halo, badge_shape=badge["shape"],
        badge_size=badge["size"], badge_font_size=badge["fontSize"],
        callout_font_size=callout["fontSize"], callout_padding=callout["padding"],
        callout_leader_width=callout["leaderWidth"], callout_max_width=callout["maxWidth"],
        arrow_width=arrow["width"], arrow_head=arrow["head"], spotlight_dim=dim,
        caption_font_size=caption["fontSize"], caption_bar_height=caption["barHeight"],
        title_font_size=title["fontSize"], title_sub_font_size=title["subFontSize"],
    )


def _builtin_colors(primary: str, text_on_primary: str) -> dict[str, str]:
    return {
        "badge": primary, "badgeText": text_on_primary,
        "callout": "#ffffff", "calloutText": "#1a1a1a",
        "highlight": primary, "arrow": primary,
        "spotlight": "#000000",
        "captionBar": "#111111", "captionText": "#ffffff",
        "titleCard": "#14213d", "titleText": "#ffffff",
    }


def _builtin_style(name: str, badge: str, badge_text: str) -> dict[str, Any]:
    return {
        "name": name,
        "refWidth": 720,
        "fonts": {"family": "IBM Plex Sans", "regular": "IBMPlexSans-Regular.ttf",
                  "semibold": "IBMPlexSans-SemiBold.ttf"},
        "colors": _builtin_colors(badge, badge_text),
        "stroke": 3, "halo": 3,
        "badge": {"shape": "circle", "size": 28, "fontSize": 16},
        "callout": {"fontSize": 15, "padding": 10, "leaderWidth": 2, "maxWidth": 280},
        "arrow": {"width": 3, "head": 12},
        "spotlight": {"dim": 0.6},
        "caption": {"fontSize": 16, "barHeight": 44},
        "title": {"fontSize": 40, "subFontSize": 20},
    }


BUILTIN_STYLES: dict[str, dict[str, Any]] = {
    "kb": _builtin_style("kb", "#b8860b", "#ffffff"),
    "review": _builtin_style("review", "#c0392b", "#ffffff"),
}

BUILTIN_STYLE_NAMES = tuple(sorted(BUILTIN_STYLES))


def builtin_style(name: str) -> Style:
    try:
        raw = BUILTIN_STYLES[name]
    except KeyError:
        raise SpecError(f"unknown style {name!r} (built-ins: {', '.join(BUILTIN_STYLE_NAMES)}).") from None
    return parse_style(raw)


def normalize_asset_name(raw: str | None, filename: str) -> str:
    candidate = (raw or "").strip() or Path(filename).stem
    if not any(char.isalnum() for char in candidate):
        raise SpecError("asset name must hold at least one letter or digit.")
    slug = slugify(candidate).strip("-._")
    if not slug:
        raise SpecError("asset name must hold at least one letter or digit.")
    return slug[:ASSET_NAME_MAX]


def check_asset_filename(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ASSET_SUFFIXES:
        raise SpecError(f"asset type must be one of {sorted(ASSET_SUFFIXES)} (got {suffix or 'none'}).")
    return suffix
