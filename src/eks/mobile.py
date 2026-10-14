from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eks.ref import Json, JsonLike, Ref
from eks.tracing import Bound
from eks.ui import (
    TargetLike,
    Text,
    maybe_target,
    read_state,
    record,
    to_target,
    to_text,
)
from eks_harness.ir.catalog.durations import optional_positive_duration
from eks_harness.ir.time import Time


def _mapping(value: Mapping[str, Any] | None, what: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} must be a mapping of names to values, got {type(value).__name__} {value!r}")
    return dict(value)


class Mobile(Bound):
    __slots__ = ()

    def dispatch(self, action: Mapping[str, JsonLike] | Ref[Any], *, at: Time | None = None) -> Ref[Json]:
        if not isinstance(action, Ref | Mapping):
            raise TypeError(
                f"s.mobile.dispatch action must be a mapping such as {{'type': 'cart/clear'}} or a Ref, "
                f"got {type(action).__name__} {action!r}"
            )
        return record(self.session, "mobile.dispatch", at, action=action if isinstance(action, Ref) else dict(action))

    def state(self, path: str | None = None) -> Ref[Json]:
        return read_state(self.session, path)

    def patch(
        self, target: TargetLike, *, props: Mapping[str, JsonLike] | None = None, style: Mapping[str, JsonLike] | None = None,
        text: Text | None = None, key: str | None = None, at: Time | None = None,
    ) -> Ref[None]:
        return record(self.session, "mobile.patch", at, target=to_target(self.session, target, "s.mobile.patch target"),
                      props=_mapping(props, "s.mobile.patch props"), style=_mapping(style, "s.mobile.patch style"),
                      text=None if text is None else to_text(text, "s.mobile.patch text"), key=key)

    def unpatch(self, key: str, *, at: Time | None = None) -> Ref[None]:
        return record(self.session, "mobile.unpatch", at, key=key)

    def mount(
        self, component: str, *, props: Mapping[str, JsonLike] | None = None, target: TargetLike | None = None,
        key: str | None = None, at: Time | None = None,
    ) -> Ref[None]:
        return record(self.session, "mobile.mount", at, component=component,
                      props=_mapping(props, "s.mobile.mount props"),
                      target=maybe_target(self.session, target, "s.mobile.mount target"), key=key)

    def unmount(self, key: str, *, at: Time | None = None) -> Ref[None]:
        return record(self.session, "mobile.unmount", at, key=key)

    def invoke(self, target: TargetLike, prop: str, *args: JsonLike, at: Time | None = None) -> Ref[Json]:
        return record(self.session, "mobile.invoke", at,
                      target=to_target(self.session, target, "s.mobile.invoke target"), prop=prop, args=args)

    def toggle(
        self, target: TargetLike, value: bool | None = None, *, timeout: Time | None = None, at: Time | None = None
    ) -> Ref[None]:
        return record(self.session, "mobile.toggle", at,
                      target=to_target(self.session, target, "s.mobile.toggle target"), value=value,
                      timeout=optional_positive_duration(timeout, "s.mobile.toggle timeout"))

    def submit(self, target: TargetLike, *, timeout: Time | None = None, at: Time | None = None) -> Ref[None]:
        return record(self.session, "mobile.submit", at,
                      target=to_target(self.session, target, "s.mobile.submit target"),
                      timeout=optional_positive_duration(timeout, "s.mobile.submit timeout"))

    def idle(self, *, timeout: Time | None = None) -> Ref[None]:
        return record(self.session, "mobile.idle", timeout=optional_positive_duration(timeout, "s.mobile.idle timeout"))

    def route_names(self) -> Ref[Json]:
        return record(self.session, "mobile.route_names")

    def logout(self, *, at: Time | None = None) -> Ref[None]:
        return record(self.session, "mobile.logout", at)

    def close_dev_menu(self) -> Ref[None]:
        return record(self.session, "mobile.close_dev_menu")

    def pointer(
        self, target: TargetLike | None = None, *, duration: Time | None = None, at: Time | None = None
    ) -> Ref[None]:
        return record(self.session, "mobile.pointer", at,
                      target=maybe_target(self.session, target, "s.mobile.pointer target"),
                      duration=optional_positive_duration(duration, "s.mobile.pointer duration"))


__all__ = ["Mobile"]
