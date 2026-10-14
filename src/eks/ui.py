from __future__ import annotations

import collections.abc
import types
import typing
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAliasType, get_args, get_origin

import typing_extensions

from eks.ref import Json, JsonLike, Ref
from eks.tracing import Bound, SessionCore
from eks_harness.ir.catalog.durations import optional_positive_duration
from eks_harness.ir.ops.ui import ScrollEdge, SwipeDirection, WaitState
from eks_harness.ir.target import Target, TargetKind, ensure_target
from eks_harness.ir.time import Time, optional_time

TargetLike = Target | str
Text = str | Ref[str] | Ref[Json]

SCROLL_EDGES: frozenset[str] = frozenset(get_args(ScrollEdge))


SEQUENCE_ORIGINS = (list, tuple, collections.abc.Sequence)


ALIAS_TYPES = (TypeAliasType, typing_extensions.TypeAliasType)


def substituted_alias(alias: TypeAliasType | typing_extensions.TypeAliasType, args: tuple[Any, ...]) -> object:
    value: Any = alias.__value__
    bindings: dict[object, Any] = dict(zip(alias.__type_params__, args, strict=False))
    if isinstance(value, typing.TypeVar):
        return bindings.get(value, value)
    parameters: tuple[object, ...] = getattr(value, "__parameters__", ())
    if parameters and all(parameter in bindings for parameter in parameters):
        return value[tuple(bindings[parameter] for parameter in parameters)]
    return value


def annotation_members(annotation: object) -> list[object]:
    if isinstance(annotation, ALIAS_TYPES):
        return annotation_members(annotation.__value__)
    origin = get_origin(annotation)
    if isinstance(origin, ALIAS_TYPES):
        return annotation_members(substituted_alias(origin, get_args(annotation)))
    if origin is Annotated:
        return annotation_members(get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        return [member for arg in get_args(annotation) for member in annotation_members(arg)]
    return [annotation]


def coerce_target_arg(value: object, annotation: object, what: str, kinds: Mapping[str, TargetKind]) -> object:
    members = annotation_members(annotation)
    if Target in members and value is not None and not isinstance(value, Mapping | Ref):
        literals = {literal for member in members if get_origin(member) is Literal for literal in get_args(member)}
        if isinstance(value, str) and value in literals:
            return value
        return ensure_target(value, what, kinds)
    if isinstance(value, list | tuple) and any(
        get_origin(member) in SEQUENCE_ORIGINS and any(Target in annotation_members(arg) for arg in get_args(member))
        for member in members
    ):
        items = [
            ensure_target(item, f"{what}[{index}]", kinds) if isinstance(item, str | Target) else item
            for index, item in enumerate(value)
        ]
        return tuple(items) if isinstance(value, tuple) else items
    return value


def to_target(session: SessionCore, value: object, what: str) -> Target:
    return ensure_target(value, what, session.ctx.registry.target_kinds())


def maybe_target(session: SessionCore, value: object, what: str) -> Target | None:
    return None if value is None else to_target(session, value, what)


def to_text(value: object, what: str) -> Text:
    if isinstance(value, str | Ref):
        return value
    raise TypeError(
        f"{what} must be a string or a Ref, got {type(value).__name__} {value!r}; "
        "interpolate other values with an f-string"
    )


def fields(**values: Any) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def record(session: SessionCore, op: str, at: Time | None = None, /, **values: Any) -> Ref[Any]:
    return session.record_op(op, fields(**values), at=optional_time(at, f"{op} at"))


def read_state(session: SessionCore, path: str | None) -> Ref[Json]:
    if path is not None and not isinstance(path, str):
        raise TypeError(
            f"the state path must be a dotted string such as 'cart.items', got {type(path).__name__} {path!r}"
        )
    return record(session, "ui.state", path=path)


class Ui(Bound):
    __slots__ = ()

    def nav(
        self, route: Text, params: Mapping[str, JsonLike] | None = None, *, timeout: Time | None = None,
        at: Time | None = None,
    ) -> Ref[None]:
        return record(self.session, "ui.nav", at, route=to_text(route, "s.ui.nav route"),
                      params=None if params is None else dict(params),
                      timeout=optional_positive_duration(timeout, "s.ui.nav timeout"))

    def back(self, *, timeout: Time | None = None, at: Time | None = None) -> Ref[None]:
        return record(self.session, "ui.back", at, timeout=optional_positive_duration(timeout, "s.ui.back timeout"))

    def reload(self, *, timeout: Time | None = None, at: Time | None = None) -> Ref[None]:
        return record(self.session, "ui.reload", at, timeout=optional_positive_duration(timeout, "s.ui.reload timeout"))

    def open_url(self, url: Text, *, timeout: Time | None = None, at: Time | None = None) -> Ref[None]:
        return record(self.session, "ui.open_url", at, url=to_text(url, "s.ui.open_url url"),
                      timeout=optional_positive_duration(timeout, "s.ui.open_url timeout"))

    def deeplink(self, url: Text, *, timeout: Time | None = None, at: Time | None = None) -> Ref[None]:
        return record(self.session, "ui.deeplink", at, url=to_text(url, "s.ui.deeplink url"),
                      timeout=optional_positive_duration(timeout, "s.ui.deeplink timeout"))

    def tap(
        self, target: TargetLike, *, force: bool = False, real: bool | None = None, timeout: Time | None = None,
        settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("tap", at, real, timeout, settle, target=self._target(target, "tap target"), force=force)

    def double_tap(
        self, target: TargetLike, *, real: bool | None = None, timeout: Time | None = None, settle: bool = True,
        at: Time | None = None,
    ) -> Ref[None]:
        return self._act("double_tap", at, real, timeout, settle, target=self._target(target, "double_tap target"))

    def long_press(
        self, target: TargetLike, *, hold: Time | None = None, real: bool | None = None,
        timeout: Time | None = None, settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("long_press", at, real, timeout, settle, target=self._target(target, "long_press target"),
                         hold=optional_positive_duration(hold, "s.ui.long_press hold"))

    def hover(
        self, target: TargetLike, *, real: bool | None = None, timeout: Time | None = None, settle: bool = True,
        at: Time | None = None,
    ) -> Ref[None]:
        return self._act("hover", at, real, timeout, settle, target=self._target(target, "hover target"))

    def fill(
        self, target: TargetLike, value: Text, *, submit: bool = False, real: bool | None = None,
        timeout: Time | None = None, settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("fill", at, real, timeout, settle, target=self._target(target, "fill target"),
                         value=to_text(value, "s.ui.fill value"), submit=submit)

    def type(
        self, target: TargetLike, value: Text, *, pace: str | None = None, submit: bool = False,
        real: bool | None = None, timeout: Time | None = None, settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("type", at, real, timeout, settle, target=self._target(target, "type target"),
                         value=to_text(value, "s.ui.type value"), pace=_pace_name(pace), submit=submit)

    def clear(
        self, target: TargetLike, *, real: bool | None = None, timeout: Time | None = None, settle: bool = True,
        at: Time | None = None,
    ) -> Ref[None]:
        return self._act("clear", at, real, timeout, settle, target=self._target(target, "clear target"))

    def key(
        self, key: str, target: TargetLike | None = None, *, real: bool | None = None,
        timeout: Time | None = None, settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("key", at, real, timeout, settle, key=key,
                         target=maybe_target(self.session, target, "s.ui.key target"))

    def select(
        self, target: TargetLike, value: Text | Sequence[Text], *, real: bool | None = None,
        timeout: Time | None = None, settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("select", at, real, timeout, settle, target=self._target(target, "select target"),
                         value=_options(value))

    def check(
        self, target: TargetLike, on: bool = True, *, real: bool | None = None, timeout: Time | None = None,
        settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("check", at, real, timeout, settle, target=self._target(target, "check target"), on=on)

    def choose(
        self, trigger: TargetLike, option: Text, *, real: bool | None = None, timeout: Time | None = None,
        settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("choose", at, real, timeout, settle, trigger=self._target(trigger, "choose trigger"),
                         option=to_text(option, "s.ui.choose option"))

    def scroll(
        self, target: TargetLike | None = None, *, to: ScrollEdge | TargetLike | None = None,
        by: tuple[int, int] | None = None, real: bool | None = None, timeout: Time | None = None,
        settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        destination: str | Target | None = to
        if to is not None and not (isinstance(to, str) and to in SCROLL_EDGES):
            destination = self._target(to, "scroll to")
        return self._act("scroll", at, real, timeout, settle,
                         target=maybe_target(self.session, target, "s.ui.scroll target"), to=destination,
                         by=None if by is None else tuple(by))

    def swipe(
        self, direction: SwipeDirection, *, target: TargetLike | None = None, distance: float | None = None,
        real: bool | None = None, timeout: Time | None = None, settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("swipe", at, real, timeout, settle, direction=direction,
                         target=maybe_target(self.session, target, "s.ui.swipe target"), distance=distance)

    def drag(
        self, source: TargetLike, to: TargetLike, *, real: bool | None = None, timeout: Time | None = None,
        settle: bool = True, at: Time | None = None,
    ) -> Ref[None]:
        return self._act("drag", at, real, timeout, settle, source=self._target(source, "drag source"),
                         to=self._target(to, "drag to"))

    def text(self, target: TargetLike | None = None, *, timeout: Time | None = None) -> Ref[str]:
        return record(self.session, "ui.text", target=maybe_target(self.session, target, "s.ui.text target"),
                      timeout=_read_timeout(timeout, "text"))

    def value(self, target: TargetLike, *, timeout: Time | None = None) -> Ref[str]:
        return record(self.session, "ui.value", target=self._target(target, "value target"),
                      timeout=_read_timeout(timeout, "value"))

    def attr(self, target: TargetLike, name: str, *, timeout: Time | None = None) -> Ref[Json]:
        return record(self.session, "ui.attr", target=self._target(target, "attr target"), name=name,
                      timeout=_read_timeout(timeout, "attr"))

    def count(self, target: TargetLike, *, timeout: Time | None = None) -> Ref[int]:
        return record(self.session, "ui.count", target=self._target(target, "count target"),
                      timeout=_read_timeout(timeout, "count"))

    def exists(self, target: TargetLike, *, timeout: Time | None = None) -> Ref[bool]:
        return record(self.session, "ui.exists", target=self._target(target, "exists target"),
                      timeout=_read_timeout(timeout, "exists"))

    def visible(self, target: TargetLike, *, timeout: Time | None = None) -> Ref[bool]:
        return record(self.session, "ui.visible", target=self._target(target, "visible target"),
                      timeout=_read_timeout(timeout, "visible"))

    def tree(
        self, target: TargetLike | None = None, *, max_lines: int | None = None, timeout: Time | None = None
    ) -> Ref[str]:
        return record(self.session, "ui.tree", target=maybe_target(self.session, target, "s.ui.tree target"),
                      max_lines=max_lines, timeout=_read_timeout(timeout, "tree"))

    def url(self) -> Ref[str]:
        return record(self.session, "ui.url")

    def route(self) -> Ref[Json]:
        return record(self.session, "ui.route")

    def state(self, path: str | None = None) -> Ref[Json]:
        return read_state(self.session, path)

    def layout(self, target: TargetLike, *, timeout: Time | None = None) -> Ref[Json]:
        return record(self.session, "ui.layout", target=self._target(target, "layout target"),
                      timeout=_read_timeout(timeout, "layout"))

    def wait_for(
        self, target: TargetLike, *, state: WaitState = "visible", timeout: Time | None = None
    ) -> Ref[None]:
        return record(self.session, "ui.wait_for", target=self._target(target, "wait_for target"), state=state,
                      timeout=optional_positive_duration(timeout, "s.ui.wait_for timeout"))

    def wait_gone(self, target: TargetLike, *, timeout: Time | None = None) -> Ref[None]:
        return record(self.session, "ui.wait_gone", target=self._target(target, "wait_gone target"),
                      timeout=optional_positive_duration(timeout, "s.ui.wait_gone timeout"))

    def wait_url(self, pattern: Text, *, timeout: Time | None = None) -> Ref[str]:
        return record(self.session, "ui.wait_url", pattern=to_text(pattern, "s.ui.wait_url pattern"),
                      timeout=optional_positive_duration(timeout, "s.ui.wait_url timeout"))

    def settle(self, *, strict: bool = True, timeout: Time | None = None) -> Ref[None]:
        return record(self.session, "ui.settle", strict=strict, timeout=optional_positive_duration(timeout, "s.ui.settle timeout"))

    def _act(
        self, name: str, at: Time | None, real: bool | None, timeout: Time | None, settle: bool, **values: Any
    ) -> Ref[None]:
        return record(self.session, f"ui.{name}", at, **values, real=real,
                      timeout=optional_positive_duration(timeout, f"s.ui.{name} timeout"), settle=settle)

    def _target(self, value: object, what: str) -> Target:
        return to_target(self.session, value, f"s.ui.{what}")


def _read_timeout(timeout: object, name: str) -> Time | None:
    return optional_positive_duration(timeout, f"s.ui.{name} timeout")


def _pace_name(pace: object) -> str | None:
    if pace is None or isinstance(pace, str):
        return pace
    raise TypeError(
        f"s.ui.type pace must be the name of a pace preset, got {type(pace).__name__} {pace!r}; write pace='demo'"
    )


def _options(value: object) -> Text | tuple[Text, ...]:
    if isinstance(value, str | Ref):
        return value
    if isinstance(value, Sequence):
        options: Sequence[object] = value
        return tuple(to_text(option, f"s.ui.select value[{index}]") for index, option in enumerate(options))
    raise TypeError(
        f"s.ui.select value must be a string, a Ref or a sequence of them, got {type(value).__name__} {value!r}"
    )


__all__ = [
    "SCROLL_EDGES",
    "TargetLike",
    "Text",
    "Ui",
    "fields",
    "maybe_target",
    "read_state",
    "record",
    "to_target",
    "to_text",
]
