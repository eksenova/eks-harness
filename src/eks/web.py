from __future__ import annotations

from eks.ref import Json, Ref
from eks.tracing import Bound
from eks.ui import TargetLike, maybe_target, record
from eks_harness.ir.ops.web import LogKind
from eks_harness.ir.time import Time


class Web(Bound):
    __slots__ = ()

    def wheel(
        self, target: TargetLike | None = None, *, dx: int = 0, dy: int = 400, real: bool | None = None,
        at: Time | None = None,
    ) -> Ref[None]:
        return record(self.session, "web.wheel", at, target=maybe_target(self.session, target, "s.web.wheel target"),
                      dx=dx, dy=dy, real=real)

    def viewport(self, name: str, *, at: Time | None = None) -> Ref[None]:
        return record(self.session, "web.viewport", at, name=name)

    def logs(self, kind: LogKind = "console", *, limit: int = 100) -> Ref[Json]:
        return record(self.session, "web.logs", kind=kind, limit=limit)

    def clear_logs(self) -> Ref[None]:
        return record(self.session, "web.clear_logs")


__all__ = ["Web"]
