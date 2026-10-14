from __future__ import annotations

from typing import Any

from eks.ref import Encodable, JsonLike, Media, Ref, type_of
from eks.tracing import Bound
from eks_harness.ir.ops.fake import FakeMode
from eks_harness.ir.time import Time, ensure_time

_MEDIA_TYPES = frozenset({"media", "json"})


def _media(value: object) -> object:
    if isinstance(value, Ref):
        produced = type_of(value)
        if produced not in _MEDIA_TYPES:
            raise TypeError(
                f"fake.arm media= expects a media ref, got {value!r} of type {produced}; "
                "load the file with eks.media.source(...)"
            )
        return value
    if isinstance(value, str):
        raise TypeError(
            f"fake.arm media= expects a media ref, got the string {value!r}; pass eks.media.source({value!r}), "
            "or fixture= for a file of the app's fixture directories"
        )
    if isinstance(value, Encodable):
        return value
    raise TypeError(f"fake.arm media= expects a media ref from eks.media.source(...), got {type(value).__name__} {value!r}")


class Fake(Bound):
    __slots__ = ()

    def arm(
        self, slot: str, *, mode: FakeMode = "success", media: Media | None = None, fixture: str | None = None,
        data: JsonLike = None, at: Time | None = None,
    ) -> Ref[None]:
        if media is not None and fixture is not None:
            raise TypeError(f"fake.arm {slot!r} takes either media= or fixture=, not both")
        if fixture is not None and not isinstance(fixture, str):
            raise TypeError(f"fake.arm fixture= expects a file name, got {type(fixture).__name__} {fixture!r}")
        args: dict[str, Any] = {"slot": slot, "mode": mode}
        if media is not None:
            args["media"] = _media(media)
        if fixture is not None:
            args["fixture"] = fixture
        if data is not None:
            args["data"] = data
        return self.session.record_op("fake.arm", args, at=ensure_time(at, "at") if at is not None else None)

    def clear(self, slot: str | None = None, *, at: Time | None = None) -> Ref[None]:
        args: dict[str, Any] = {}
        if slot is not None:
            args["slot"] = slot
        return self.session.record_op("fake.clear", args, at=ensure_time(at, "at") if at is not None else None)


__all__ = ["Fake"]
