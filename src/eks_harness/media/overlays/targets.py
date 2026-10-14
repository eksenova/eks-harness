from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel, JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.ops.overlay import OverlayBase
from eks_harness.ir.target import Target, describe
from eks_harness.media.overlays.boxmap import Box, BoxMap, BoxNode, as_boxmap

MOBILE_ROLE_KINDS: Mapping[str, str] = {
    "button": "button", "link": "button", "textbox": "input", "searchbox": "input", "switch": "switch",
    "checkbox": "switch", "heading": "text", "text": "text",
}
COORDINATE_KINDS = frozenset({"point", "rect"})
ANCHOR_FIELDS = ("anchor", "to")
MAX_LISTED = 10

TargetLike = Target | Mapping[str, Any]


def _dump(target: TargetLike) -> dict[str, Any]:
    if isinstance(target, BaseModel):
        return target.model_dump(mode="json", by_alias=True, exclude_none=True)
    return dict(target)


def _describe(target: Mapping[str, Any]) -> str:
    try:
        return describe(Target.model_validate(target))
    except (ValueError, TypeError):
        return str(dict(target))


def clean(text: str) -> str:
    return " ".join(text.split())


def fold(text: str) -> str:
    lowered = text.casefold().replace("ı", "i")
    stripped = "".join(char for char in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(char))
    return clean(stripped)


def text_matches(value: str | None, needle: str, exact: bool) -> bool:
    if value is None:
        return False
    if exact:
        return clean(value) == clean(needle)
    return fold(needle) in fold(value)


def _flag(args: Mapping[str, Any], name: str, default: bool) -> bool:
    value = args.get(name, default)
    return value if isinstance(value, bool) else default


def _native_role(node: BoxNode, role: str) -> bool:
    from eks_harness.drivers.native import ROLE_CLASSES

    classes = ROLE_CLASSES.get(role, ())
    return any((node.kind or "").endswith(name) for name in classes)


def _matches(node: BoxNode, by: str, args: Mapping[str, Any], source: str) -> bool:
    if by == "test_id":
        return node.test_id is not None and node.test_id == str(args.get("id"))
    if by == "kind":
        return node.kind is not None and node.kind == str(args.get("kind"))
    if by == "text":
        value = node.text if source != "native" else (node.text or node.label)
        return text_matches(value, str(args.get("text")), _flag(args, "exact", True))
    if by == "label":
        value = node.label if source == "web" else (node.label or node.text)
        return text_matches(value, str(args.get("text")), _flag(args, "exact", False))
    if by == "placeholder":
        return text_matches(node.placeholder, str(args.get("text")), _flag(args, "exact", False))
    if by == "role":
        role = str(args.get("role"))
        name = args.get("name")
        exact = _flag(args, "exact", False)
        if source == "app":
            kind = MOBILE_ROLE_KINDS.get(role)
            if kind is None or node.kind != kind:
                return False
            return name is None or text_matches(node.label or node.text, str(name), exact)
        if source == "native":
            if not _native_role(node, role):
                return False
            return name is None or text_matches(node.text or node.label, str(name), exact)
        if node.role != role:
            return False
        return name is None or text_matches(node.name, str(name), exact)
    raise NodeFailure("target_unsupported", f"targets by {by} cannot be found on a box map",
                      detail={"by": by})


def _coordinates(by: str, args: Mapping[str, Any]) -> Box:
    x, y = float(args.get("x", 0)), float(args.get("y", 0))
    if by == "point":
        return (x, y, 0.0, 0.0)
    return (x, y, float(args.get("w", 0)), float(args.get("h", 0)))


def node_text(node: BoxNode) -> str:
    head = f"{node.kind or 'node'}{f'#{node.test_id}' if node.test_id else ''}"
    shown = node.text or node.name or node.label or node.placeholder
    x, y, w, h = node.box
    label = f' "{shown[:60]}"' if shown else ""
    return f"{head}{label} at {round(x)},{round(y)} {round(w)}x{round(h)}"


def _outermost(boxes: BoxMap, matches: list[int]) -> list[int]:
    found = set(matches)
    return [index for index in matches if not any(parent in found for parent in boxes.ancestors(index))]


def _inside(node: BoxNode, area: Box) -> bool:
    cx, cy = node.centre
    x, y, w, h = area
    return x <= cx <= x + w and y <= cy <= y + h


def _near_misses(boxes: BoxMap, args: Mapping[str, Any]) -> list[JsonValue]:
    needle = args.get("text") or args.get("name") or args.get("id")
    if not isinstance(needle, str) or not needle.strip():
        return []
    words = [word for word in fold(needle).split() if len(word) > 1] or [fold(needle)]
    found: list[JsonValue] = []
    for node in boxes.nodes:
        haystack = fold(" ".join(part for part in (node.text, node.name, node.label, node.placeholder, node.test_id)
                                 if part))
        if haystack and any(word in haystack for word in words):
            found.append(node_text(node))
            if len(found) >= MAX_LISTED:
                break
    return found


def _candidates(target: Mapping[str, Any], boxes: BoxMap) -> list[int]:
    by = str(target.get("by"))
    if by == "css":
        raise NodeFailure("target_unsupported",
                          f"{_describe(target)} is a CSS target, which needs the live DOM; anchor overlays on a "
                          "captured image by test id, text, role, label, placeholder, point or rect",
                          detail={"target": cast(JsonValue, dict(target))})
    raw_args: object = target.get("args") or {}
    args: Mapping[str, Any] = cast(Mapping[str, Any], raw_args) if isinstance(raw_args, Mapping) else {}
    if by in COORDINATE_KINDS:
        area = _coordinates(by, args)
        return [index for index, node in enumerate(boxes.nodes) if _inside(node, area)] if by == "rect" else []
    matches = [index for index, node in enumerate(boxes.nodes) if _matches(node, by, args, boxes.source)]
    within = target.get("within")
    if isinstance(within, Mapping):
        scope = cast(Mapping[str, Any], within)
        if scope.get("by") in COORDINATE_KINDS:
            scope_args = cast(Mapping[str, Any], scope.get("args") or {})
            area = _coordinates(str(scope.get("by")), scope_args)
            matches = [index for index in matches if _inside(boxes.nodes[index], area)]
        else:
            scopes = set(_scope(scope, boxes))
            matches = [index for index in matches if any(parent in scopes for parent in boxes.ancestors(index))]
    matches = _outermost(boxes, matches)
    near = target.get("near")
    if isinstance(near, Mapping) and matches:
        anchor = _box(cast(Mapping[str, Any], near), boxes)
        ax, ay = anchor[0] + anchor[2] / 2, anchor[1] + anchor[3] / 2
        matches.sort(key=lambda index: math.hypot(boxes.nodes[index].centre[0] - ax,
                                                  boxes.nodes[index].centre[1] - ay))
    return matches


def _scope(target: Mapping[str, Any], boxes: BoxMap) -> list[int]:
    matches = _candidates(target, boxes)
    nth = target.get("nth")
    if isinstance(nth, int) and not isinstance(nth, bool):
        index = nth if nth >= 0 else len(matches) + nth
        return [matches[index]] if 0 <= index < len(matches) else []
    return matches


def _box(target: Mapping[str, Any], boxes: BoxMap) -> Box:
    by = str(target.get("by"))
    if by in COORDINATE_KINDS:
        return _coordinates(by, cast(Mapping[str, Any], target.get("args") or {}))
    matches = _candidates(target, boxes)
    shown = _describe(target)
    detail: dict[str, JsonValue] = {"target": cast(JsonValue, dict(target)), "count": len(matches)}
    if not matches:
        args = cast(Mapping[str, Any], target.get("args") or {})
        misses = _near_misses(boxes, args)
        raise NodeFailure("target_not_found",
                          f"no element of the captured box map matches {shown}"
                          + (f"; near misses: {', '.join(str(item) for item in misses)}" if misses else ""),
                          detail={**detail, "nearMisses": misses})
    nth = target.get("nth")
    if not isinstance(nth, int) or isinstance(nth, bool):
        if len(matches) > 1 and not isinstance(target.get("near"), Mapping):
            candidates: list[JsonValue] = [node_text(boxes.nodes[index]) for index in matches[:MAX_LISTED]]
            raise NodeFailure("target_ambiguous",
                              f"{len(matches)} elements of the captured box map match {shown}; add nth, first or "
                              "last, or narrow it with within or near",
                              detail={**detail, "candidates": candidates})
        nth = 0
    index = nth if nth >= 0 else len(matches) + nth
    if not 0 <= index < len(matches):
        raise NodeFailure("target_not_found", f"{shown} asks for match {nth} but {len(matches)} match",
                          detail=detail)
    return boxes.nodes[matches[index]].box


def resolve_on_boxmap(target_dump: TargetLike, boxmap: BoxMap | Mapping[str, Any]) -> tuple[float, float, float, float]:
    return _box(_dump(target_dump), as_boxmap(boxmap))


def overlay_targets(overlay: OverlayBase) -> dict[str, Target]:
    found: dict[str, Target] = {}
    for name in ANCHOR_FIELDS:
        value: object = getattr(overlay, name, None)
        if isinstance(value, Target):
            found[name] = value
    return found


def needs_boxmap(targets: Sequence[Target]) -> bool:
    return any(_needs(target) for target in targets)


def _needs(target: Target) -> bool:
    return target.by not in COORDINATE_KINDS


__all__ = [
    "MOBILE_ROLE_KINDS",
    "clean",
    "fold",
    "needs_boxmap",
    "node_text",
    "overlay_targets",
    "resolve_on_boxmap",
    "text_matches",
]
