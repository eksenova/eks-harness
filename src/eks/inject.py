from __future__ import annotations

from typing import Any

from eks.ref import Json, JsonLike, Ref
from eks.tracing import Bound, SessionCore, TraceError
from eks.ui import Text
from eks_harness.ir.ops.inject import When
from eks_harness.ir.time import Time, ensure_time


def _source(value: object, what: str) -> Text:
    if isinstance(value, str | Ref):
        return value
    raise TypeError(f"{what} expects source text (a str, an f-string or a Ref), got {type(value).__name__} {value!r}")


class Inject(Bound):
    __slots__ = ()

    def script(self, source: Text, *, when: When = "now", at: Time | None = None) -> Ref[None]:
        if when == "init" and self.session.platform != "web":
            raise TraceError(
                f"inject.script when='init' runs before page scripts and exists only on web; "
                f"this {self.session.platform} session can use when='now'"
            )
        return self.session.record_op(
            "inject.script", {"source": _source(source, "inject.script"), "when": when},
            at=ensure_time(at, "at") if at is not None else None)

    def style(self, css: Text, *, at: Time | None = None) -> Ref[None]:
        return self.session.record_op(
            "inject.style", {"css": _source(css, "inject.style")}, at=ensure_time(at, "at") if at is not None else None)


def evaluate(session: SessionCore, source: Text, arg: JsonLike = None) -> Ref[Json]:
    args: dict[str, Any] = {"source": _source(source, "eval")}
    if arg is not None:
        args["arg"] = arg
    return session.record_op("inject.eval", args)


__all__ = ["Inject", "evaluate"]
