from __future__ import annotations

from typing import Any

from eks.ref import Json, Ref
from eks.tracing import Bound


class Debug(Bound):
    __slots__ = ()

    def break_(self, label: str, *, when: Ref[bool] | Ref[Json] | None = None) -> Ref[None]:
        if not isinstance(label, str):
            raise TypeError(f"break_ label is a plain string such as 'before-submit', got {type(label).__name__}")
        args: dict[str, Any] = {"label": label}
        if when is not None:
            if not isinstance(when, Ref):
                raise TypeError(
                    f"break_ when= takes a Ref produced earlier (for example s.ui.exists('#error')), "
                    f"got {type(when).__name__} {when!r}; leave it out to always break"
                )
            args["when"] = when
        return self.session.record_op("debug.break", args)

    def pause_on_error(self, on: bool = True) -> Ref[None]:
        if not isinstance(on, bool):
            raise TypeError(f"pause_on_error takes True or False, got {type(on).__name__} {on!r}")
        return self.session.record_op("debug.pause_on_error", {"on": on})


__all__ = ["Debug"]
