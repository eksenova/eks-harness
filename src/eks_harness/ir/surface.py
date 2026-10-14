from __future__ import annotations

import json
import math
import types
import typing
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Any, TypeAliasType, cast, get_args, get_origin

import typing_extensions
from pydantic import BaseModel, JsonValue, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, to_jsonable_python

from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.target import (
    BUILTIN_KINDS,
    Target,
    TargetError,
    describe,
    parse_target,
)
from eks_harness.ir.time import Time


def _frozen(values: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return MappingProxyType(dict(values or {}))


@dataclass(frozen=True)
class SurfaceEntry:
    path: str
    positional: tuple[str, ...] = ()
    keyword_renames: Mapping[str, str] = field(default_factory=_frozen)
    opaque: frozenset[str] = frozenset()
    keywords: frozenset[str] | None = None
    attribute: bool = False


def _entry(path: str, *positional: str, renames: Mapping[str, str] | None = None, opaque: Sequence[str] = (),
           keywords: Sequence[str] | None = None, attribute: bool = False) -> SurfaceEntry:
    return SurfaceEntry(path=path, positional=positional, keyword_renames=_frozen(renames), opaque=frozenset(opaque),
                        keywords=None if keywords is None else frozenset(keywords), attribute=attribute)


_API_RENAMES = {"persona": "as_"}

_ENTRIES: dict[str, SurfaceEntry] = {
    "ui.nav": _entry("s.ui.nav", "route", "params"),
    "ui.back": _entry("s.ui.back"),
    "ui.reload": _entry("s.ui.reload"),
    "ui.open_url": _entry("s.ui.open_url", "url"),
    "ui.deeplink": _entry("s.ui.deeplink", "url"),
    "ui.tap": _entry("s.ui.tap", "target"),
    "ui.double_tap": _entry("s.ui.double_tap", "target"),
    "ui.long_press": _entry("s.ui.long_press", "target"),
    "ui.hover": _entry("s.ui.hover", "target"),
    "ui.fill": _entry("s.ui.fill", "target", "value"),
    "ui.type": _entry("s.ui.type", "target", "value"),
    "ui.clear": _entry("s.ui.clear", "target"),
    "ui.key": _entry("s.ui.key", "key", "target"),
    "ui.select": _entry("s.ui.select", "target", "value"),
    "ui.check": _entry("s.ui.check", "target", "on"),
    "ui.choose": _entry("s.ui.choose", "trigger", "option"),
    "ui.scroll": _entry("s.ui.scroll", "target"),
    "ui.swipe": _entry("s.ui.swipe", "direction"),
    "ui.drag": _entry("s.ui.drag", "source", "to"),
    "ui.text": _entry("s.ui.text", "target"),
    "ui.value": _entry("s.ui.value", "target"),
    "ui.attr": _entry("s.ui.attr", "target", "name"),
    "ui.count": _entry("s.ui.count", "target"),
    "ui.exists": _entry("s.ui.exists", "target"),
    "ui.visible": _entry("s.ui.visible", "target"),
    "ui.tree": _entry("s.ui.tree", "target"),
    "ui.url": _entry("s.ui.url"),
    "ui.route": _entry("s.ui.route"),
    "ui.state": _entry("s.ui.state", "path"),
    "ui.layout": _entry("s.ui.layout", "target"),
    "ui.wait_for": _entry("s.ui.wait_for", "target"),
    "ui.wait_gone": _entry("s.ui.wait_gone", "target"),
    "ui.wait_url": _entry("s.ui.wait_url", "pattern"),
    "ui.settle": _entry("s.ui.settle"),
    "web.wheel": _entry("s.web.wheel", "target"),
    "web.viewport": _entry("s.web.viewport", "name"),
    "web.logs": _entry("s.web.logs", "kind"),
    "web.clear_logs": _entry("s.web.clear_logs"),
    "mobile.dispatch": _entry("s.mobile.dispatch", "action"),
    "mobile.patch": _entry("s.mobile.patch", "target"),
    "mobile.unpatch": _entry("s.mobile.unpatch", "key"),
    "mobile.mount": _entry("s.mobile.mount", "component"),
    "mobile.unmount": _entry("s.mobile.unmount", "key"),
    "mobile.invoke": _entry("s.mobile.invoke", "target", "prop", "*args"),
    "mobile.toggle": _entry("s.mobile.toggle", "target", "value"),
    "mobile.submit": _entry("s.mobile.submit", "target"),
    "mobile.idle": _entry("s.mobile.idle"),
    "mobile.route_names": _entry("s.mobile.route_names"),
    "mobile.logout": _entry("s.mobile.logout"),
    "mobile.close_dev_menu": _entry("s.mobile.close_dev_menu"),
    "mobile.pointer": _entry("s.mobile.pointer", "target"),
    "api.get": _entry("s.api.get", "path", renames=_API_RENAMES),
    "api.delete": _entry("s.api.delete", "path", renames=_API_RENAMES),
    "api.post": _entry("s.api.post", "path", renames=_API_RENAMES),
    "api.put": _entry("s.api.put", "path", renames=_API_RENAMES),
    "api.patch": _entry("s.api.patch", "path", renames=_API_RENAMES),
    "backend.info": _entry("s.backend.info", attribute=True),
    "session.as": _entry("s.as_", "persona"),
    "net.route": _entry("s.net.route", "method", "pattern"),
    "net.unroute": _entry("s.net.unroute", "pattern"),
    "net.shape": _entry("s.net.shape", "profile"),
    "net.cassette": _entry("s.net.cassette", "name"),
    "inject.script": _entry("s.inject.script", "source"),
    "inject.style": _entry("s.inject.style", "css"),
    "inject.eval": _entry("s.eval", "source", "arg"),
    "capture.shot": _entry("s.shot", "name"),
    "capture.dump": _entry("s.dump", "kind"),
    "capture.har": _entry("s.har"),
    "capture.hold": _entry("s.hold", "duration"),
    "capture.record": _entry("s.record", "name"),
    "check.checkpoint": _entry("s.checkpoint", "label"),
    "check.explore": _entry("s.explore", "budget"),
    "overlay.show": _entry("s.overlay.show", "overlay"),
    "overlay.remove": _entry("s.overlay.remove", "key"),
    "overlay.clear": _entry("s.overlay.clear", "kind"),
    "overlay.annotate": _entry("eks.annotate", "artifact", "overlays", opaque=("artifact",)),
    "fake.arm": _entry("s.fake.arm", "slot", opaque=("media",)),
    "fake.clear": _entry("s.fake.clear", "slot"),
    "expect.target": _entry("expect", "target"),
    "expect.value": _entry("expect", "value", opaque=("value",)),
    "debug.break": _entry("s.debug.break_", "label", opaque=("when",)),
    "debug.pause_on_error": _entry("s.debug.pause_on_error", "on"),
    "data.create": _entry("d.create", "kind", "key", opaque=("path",)),
    "data.base": _entry("d.base", "path", opaque=("base",)),
    "data.extend": _entry("d.extend", "base", opaque=("hash",)),
    "media.source": _entry("eks.media.source", "uri"),
    "media.tts": _entry("eks.media.tts", "text"),
    "media.stems": _entry("eks.media.stems", "media", "*stems", opaque=("media",)),
    "media.markers": _entry("eks.media.markers", "media", "*sources", opaque=("media",)),
    "gfx.render": _entry("e.gfx", "scene", opaque=("scene",)),
    "scene.render": _entry("e.scene", "scene", opaque=("scene",)),
    "ctl.when": _entry("eks.when", "cond"),
    "ctl.each": _entry("eks.each", "over"),
    "ctl.parallel": _entry("eks.parallel"),
    "ctl.dynamic": _entry("eks.dynamic", "fn", opaque=("fn",)),
}

SURFACE: Mapping[str, SurfaceEntry] = MappingProxyType(_ENTRIES)

OVERLAY_SURFACE: Mapping[str, SurfaceEntry] = MappingProxyType({
    "title": _entry("s.overlay.title", "text",
                    keywords=("subtitle", "kicker", "hold", "keep", "key", "style", "emphasis")),
    "caption": _entry("s.overlay.caption", "text",
                      keywords=("position", "step", "hold", "keep", "key", "placement", "style", "emphasis")),
    "callout": _entry("s.overlay.callout", "anchor", "text",
                      keywords=("hold", "keep", "key", "placement", "style", "emphasis")),
    "spotlight": _entry("s.overlay.spotlight", "anchor", "text",
                        keywords=("dim", "hold", "keep", "key", "placement", "style", "emphasis")),
    "highlight": _entry("s.overlay.highlight", "anchor",
                        keywords=("shape", "hold", "keep", "key", "style", "emphasis")),
    "arrow": _entry("s.overlay.arrow", "anchor", "to", "text",
                    keywords=("hold", "keep", "key", "placement", "style", "emphasis")),
    "blur": _entry("s.overlay.blur", "anchor", keywords=("radius", "hold", "keep", "key")),
    "redact": _entry("s.overlay.redact", "anchor", keywords=("hold", "keep", "key")),
    "icon": _entry("s.overlay.icon", "anchor", "name", keywords=("hold", "keep", "key", "placement", "style")),
    "image": _entry("s.overlay.image", "anchor", "image", renames={"image": "media"}, opaque=("image",),
                    keywords=("width", "hold", "keep", "key", "placement")),
    "step": _entry("s.overlay.step", "anchor", "index", keywords=("hold", "keep", "key", "placement", "style")),
})

EXEMPT: Mapping[str, str] = MappingProxyType({
    "check.media": "an edit emits it when it finishes, one per e.expect.<kind>(...) expectation; there is no call "
                   "that records it on its own",
})

_ALIAS_TYPES = (TypeAliasType, typing_extensions.TypeAliasType)
_SEQUENCES = (list, tuple, Sequence)
_MAPPINGS = (dict, Mapping)


class _Opaque(Exception):
    pass


def _members(annotation: object, seen: set[int] | None = None) -> Iterator[object]:
    seen = set() if seen is None else seen
    if id(annotation) in seen:
        return
    seen.add(id(annotation))
    if isinstance(annotation, _ALIAS_TYPES):
        yield from _members(annotation.__value__, seen)
        return
    origin = get_origin(annotation)
    if isinstance(origin, _ALIAS_TYPES):
        yield from _members(origin.__value__, seen)
        return
    if origin is Annotated:
        yield from _members(get_args(annotation)[0], seen)
        return
    if origin is typing.Union or origin is types.UnionType:
        for arg in get_args(annotation):
            yield from _members(arg, seen)
        return
    yield annotation


def _is_model(member: object) -> bool:
    return isinstance(member, type) and issubclass(member, BaseModel)


def _flat(annotations: Sequence[object]) -> list[object]:
    seen: set[int] = set()
    return [member for annotation in annotations for member in _members(annotation, seen)]


def _items(members: Sequence[object]) -> list[object]:
    return _flat([arg for member in members if get_origin(member) in _SEQUENCES
                  for arg in get_args(member) if arg is not Ellipsis])


def _takes_objects(members: Sequence[object]) -> bool:
    return any(member is Any or member in _MAPPINGS or get_origin(member) in _MAPPINGS for member in members)


def _literal(value: object) -> str:
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Opaque
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        items = cast("Mapping[object, object]", value)
        return "{" + ", ".join(f"{_literal(key)}: {_literal(item)}" for key, item in items.items()) + "}"
    if isinstance(value, list | tuple):
        sequence = cast("Sequence[object]", value)
        return "[" + ", ".join(_literal(item) for item in sequence) + "]"
    raise _Opaque


def _by_call(target: Target, registry: Registry) -> str:
    kinds = registry.target_kinds()
    kind = kinds.get(target.by)
    builtin = BUILTIN_KINDS.get(target.by)
    if kind is None or builtin is None or kind.args is not builtin.args:
        parts = [_literal(target.by), *(f"{name}={_literal(value)}" for name, value in target.args.items())]
        return f"by.custom({', '.join(parts)})"
    parts: list[str] = []
    for name, info in kind.args.model_fields.items():
        if name not in target.args:
            continue
        value = target.args[name]
        if info.is_required():
            parts.append(_literal(value))
        elif value != _default(info):
            parts.append(f"{name}={_literal(value)}")
    return f"by.{target.by}({', '.join(parts)})"


def target_text(value: object, registry: Registry | None = None) -> str:
    chosen = registry if registry is not None else default_registry()
    try:
        target = value if isinstance(value, Target) else Target.model_validate(value)
    except ValidationError:
        raise _Opaque from None
    plain = target.model_copy(update={"within_": None, "near_": None, "nth_": None, "surface_": None})
    kinds = chosen.target_kinds()
    text = describe(plain, kinds)
    try:
        shorthand = parse_target(text, kinds) == plain
    except TargetError:
        shorthand = False
    modified = (target.within_ is not None or target.near_ is not None or target.nth_ is not None
                or target.surface_ is not None)
    base = _literal(text) if shorthand and not modified else _by_call(plain, chosen)
    if target.within_ is not None:
        base += f".within({target_text(target.within_, chosen)})"
    if target.near_ is not None:
        base += f".near({target_text(target.near_, chosen)})"
    if target.nth_ is not None:
        base += f".nth({target.nth_})"
    if target.surface_ == "native":
        base += ".native"
    return base


def _render(value: object, members: Sequence[object], registry: Registry) -> str:
    if isinstance(value, Mapping) and Target in members and "by" in value:
        return target_text(value, registry)
    if isinstance(value, str) and Time in members:
        return f"t({_literal(value)})"
    if isinstance(value, list | tuple):
        items = _items(members)
        sequence = cast("Sequence[object]", value)
        return "[" + ", ".join(_render(entry, items, registry) for entry in sequence) + "]"
    if isinstance(value, Mapping) and any(_is_model(member) for member in members) and not _takes_objects(members):
        raise _Opaque
    return _literal(value)


def _field_members(info: FieldInfo) -> list[object]:
    return _flat([info.annotation])


def _default(info: FieldInfo) -> object:
    if info.default_factory is not None:
        factory = cast("Callable[[], object]", info.default_factory)
        return to_jsonable_python(factory(), by_alias=True)
    if info.default is PydanticUndefined:
        return PydanticUndefined
    return to_jsonable_python(info.default, by_alias=True)


def _fields(model: type[BaseModel]) -> dict[str, FieldInfo]:
    return {info.alias or name: info for name, info in model.model_fields.items()}


def _call(entry: SurfaceEntry, model: type[BaseModel], args: Mapping[str, JsonValue], registry: Registry,
          path: str | None = None, extra: Sequence[str] = ()) -> str:
    fields = _fields(model)
    present: dict[str, JsonValue] = {}
    for key, value in args.items():
        info = fields.get(key)
        if info is None:
            raise _Opaque
        if key in entry.opaque and value is not None:
            raise _Opaque
        if value is None or value == _default(info):
            continue
        present[key] = value
    if entry.attribute:
        if present:
            raise _Opaque
        return path or entry.path
    parts: list[str] = []
    in_order = True
    for name in entry.positional:
        splat = name.startswith("*")
        key = name.lstrip("*")
        if key not in present:
            if not splat:
                in_order = False
            continue
        value = present.pop(key)
        members = _field_members(fields[key])
        if splat:
            if not in_order or not isinstance(value, list):
                raise _Opaque
            parts.extend(_render(item, _items(members), registry) for item in value)
        elif in_order:
            parts.append(_render(value, members, registry))
        else:
            parts.append(f"{entry.keyword_renames.get(key, key)}={_render(value, members, registry)}")
    for key, value in present.items():
        if entry.keywords is not None and key not in entry.keywords:
            raise _Opaque
        rendered = _render(value, _field_members(fields[key]), registry)
        parts.append(f"{entry.keyword_renames.get(key, key)}={rendered}")
    return f"{path or entry.path}({', '.join([*parts, *extra])})"


def _expect_target(args: Mapping[str, JsonValue], registry: Registry) -> str:
    soft = args.get("soft") is True
    eventually = args.get("eventually") is True
    if soft and eventually:
        raise _Opaque
    head = "expect.soft" if soft else "expect.eventually" if eventually else "expect"
    subject = [target_text(args.get("target"), registry)]
    timeout = args.get("timeout")
    if timeout is not None:
        if not isinstance(timeout, str):
            raise _Opaque
        subject.append(f"timeout=t({_literal(timeout)})")
    assertion = args.get("assertion")
    if not isinstance(assertion, Mapping):
        raise _Opaque
    kind = assertion.get("kind")
    parts: list[str] = []
    keywords: tuple[str, ...] = ()
    match kind:
        case "visible" | "hidden" | "enabled" | "disabled":
            positional: tuple[str, ...] = ()
        case "checked":
            positional = () if assertion.get("on", True) is True else ("on",)
        case "text" | "value":
            positional, keywords = ("equals",), ("contains", "matches")
        case "count":
            positional, keywords = ("equals",), ("at_least", "at_most")
        case "attr":
            positional, keywords = ("name", "equals"), ("contains", "matches")
        case _:
            raise _Opaque
    known = {"kind", "on", *positional, *keywords}
    if any(key not in known for key in assertion):
        raise _Opaque
    in_order = True
    for name in positional:
        value = assertion.get(name)
        if value is None:
            in_order = False
        elif in_order:
            parts.append(_literal(value))
        else:
            parts.append(f"{name}={_literal(value)}")
    parts.extend(f"{name}={_literal(assertion[name])}" for name in keywords if assertion.get(name) is not None)
    message = args.get("message")
    if message is not None:
        parts.append(f"message={_literal(message)}")
    extra = set(args) - {"target", "assertion", "soft", "eventually", "timeout", "message"}
    if extra:
        raise _Opaque
    return f"{head}({', '.join(subject)}).{kind}({', '.join(parts)})"


def _overlay(args: Mapping[str, JsonValue], registry: Registry) -> str:
    if set(args) != {"overlay"}:
        raise _Opaque
    overlay = args["overlay"]
    if not isinstance(overlay, Mapping):
        raise _Opaque
    kind = overlay.get("kind")
    entry = OVERLAY_SURFACE.get(kind) if isinstance(kind, str) else None
    if entry is None or not isinstance(kind, str):
        raise _Opaque
    model = registry.catalog("overlay", kind).model
    values = {key: value for key, value in overlay.items() if key not in ("kind", "placement")}
    placement = overlay.get("placement")
    extra = [] if placement is None else [f"placement={_placement(placement)}"]
    if extra and entry.keywords is not None and "placement" not in entry.keywords:
        raise _Opaque
    return _call(entry, model, values, registry, extra=extra)


def _placement(value: JsonValue) -> str:
    if not isinstance(value, Mapping):
        raise _Opaque
    fields = {"mode": "auto", "side": None, "dx": 0.0, "dy": 0.0}
    if set(value) - set(fields):
        raise _Opaque
    merged = {**fields, **value}
    if merged["mode"] == "auto" and merged["dx"] == 0 and merged["dy"] == 0 and isinstance(merged["side"], str):
        return _literal(merged["side"])
    parts = [f"{key}={_literal(merged[key])}" for key, default in fields.items() if merged[key] != default]
    return f"eks.overlay.Placement({', '.join(parts)})"


_ROUTE_ACTIONS: Mapping[str, tuple[tuple[str, ...], Mapping[str, str]]] = MappingProxyType({
    "delay": (("by",), {}),
    "respond": ((), {"status": "status", "json": "json", "body": "body", "headers": "headers"}),
    "patch": ((), {"path": "lambda_path", "value": "value"}),
    "abort": (("reason",), {}),
    "passthrough": ((), {}),
})


def _route(args: Mapping[str, JsonValue], registry: Registry) -> str:
    del registry
    if set(args) - {"method", "pattern", "action", "times"}:
        raise _Opaque
    method = args.get("method", "GET")
    pattern = args.get("pattern")
    action = args.get("action")
    if not isinstance(pattern, str) or not isinstance(action, Mapping):
        raise _Opaque
    head = [_literal(method), _literal(pattern)]
    if args.get("times") is not None:
        head.append(f"times={_literal(args['times'])}")
    kind = action.get("kind")
    shape = _ROUTE_ACTIONS.get(kind) if isinstance(kind, str) else None
    if shape is None:
        raise _Opaque
    positional, keywords = shape
    parts: list[str] = []
    for key, value in action.items():
        if key == "kind" or value is None:
            continue
        if key in positional:
            parts.append(f"t({_literal(value)})" if kind == "delay" and isinstance(value, str) else _literal(value))
        elif key in keywords:
            if kind == "respond" and key == "status" and value == 200:
                continue
            parts.append(f"{keywords[key]}={_literal(value)}")
        else:
            raise _Opaque
    return f"s.net.route({', '.join(head)}).{kind}({', '.join(parts)})"


_CUSTOM: Mapping[str, Callable[[Mapping[str, JsonValue], Registry], str]] = MappingProxyType({
    "expect.target": _expect_target,
    "overlay.show": _overlay,
    "net.route": _route,
})


def call_text(op: str, args: Mapping[str, JsonValue], *, registry: Registry | None = None) -> str | None:
    entry = SURFACE.get(op)
    chosen = registry if registry is not None else default_registry()
    if entry is None or not chosen.has_op(op):
        return None
    spec = chosen.op(op)
    if spec.container:
        return None
    try:
        custom = _CUSTOM.get(op)
        if custom is not None:
            return custom(args, chosen)
        return _call(entry, spec.args, args, chosen)
    except (_Opaque, TargetError, KeyError):
        return None


def op_text(op: str, args: Mapping[str, JsonValue]) -> str:
    return f"s.op({_literal(op)}, {_literal(dict(args))})"


__all__ = ["EXEMPT", "OVERLAY_SURFACE", "SURFACE", "SurfaceEntry", "call_text", "op_text", "target_text"]
