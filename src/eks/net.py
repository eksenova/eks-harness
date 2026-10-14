from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, get_args

from eks.ref import JsonLike, Ref
from eks.tracing import Bound, SessionCore, TraceContext, TraceError
from eks.ui import Text
from eks_harness.ir.catalog.durations import duration_problem
from eks_harness.ir.ops.net import CassetteMode, HttpMethod
from eks_harness.ir.presets import NetProfile
from eks_harness.ir.time import Time, TimeError, ensure_duration, ensure_time

AbortReason = str

_METHODS: tuple[str, ...] = get_args(HttpMethod)
_ACTIONS = ".delay(t(...)), .respond(status=...), .patch(lambda_path=..., value=...), .abort() or .passthrough()"


def _pin(at: Time | None) -> Time | None:
    return ensure_time(at, "at") if at is not None else None


class RouteBuilder:
    def __init__(
        self, session: SessionCore, method: HttpMethod, pattern: Text, *, times: int | None, at: Time | None
    ) -> None:
        if method not in _METHODS:
            raise ValueError(f"net.route method {method!r} is not one of {list(_METHODS)}; write it in upper case")
        if times is not None and (isinstance(times, bool) or not isinstance(times, int)):
            raise TypeError(f"net.route times= expects an int count such as 1, got {type(times).__name__} {times!r}")
        self._session = session
        self._method: HttpMethod = method
        self._pattern = pattern
        self._times = times
        self._at = _pin(at)
        self._recorded: Ref[None] | None = None
        self._scope = session.ctx.scope()
        site = session.ctx.call_site()
        self._where = f" at {site.src()}" if site is not None else ""
        session.ctx.on_finalize(self._check_finished)

    def delay(self, by: Time) -> Ref[None]:
        duration = ensure_duration(by, "net.route delay")
        problem = duration_problem(duration)
        if problem is not None:
            raise TimeError(f"net.route delay for {self._describe()} {problem}")
        return self._finish({"kind": "delay", "by": duration})

    def respond(
        self, *, status: int = 200, json: JsonLike = None, body: Text | None = None,
        headers: Mapping[str, Text] | None = None,
    ) -> Ref[None]:
        if isinstance(status, bool) or not isinstance(status, int):
            raise TypeError(f"respond status= expects an int HTTP status such as 500, got {type(status).__name__} {status!r}")
        if json is not None and body is not None:
            raise TypeError("respond takes either json= (a JSON body) or body= (a text body), not both")
        action: dict[str, Any] = {"kind": "respond", "status": status}
        if json is not None:
            action["json"] = json
        if body is not None:
            action["body"] = body
        if headers is not None:
            action["headers"] = dict(headers)
        return self._finish(action)

    def patch(self, *, lambda_path: str, value: JsonLike) -> Ref[None]:
        if not isinstance(lambda_path, str) or not lambda_path:
            raise TypeError(
                f"patch lambda_path= expects a non-empty literal JSON path such as 'items[0].rate', got {lambda_path!r}"
            )
        if value is None:
            raise TypeError(
                f"patch value= for {lambda_path!r} is None, and JSON null is not supported as a patch value; "
                "pass a concrete value or use .respond(json=...) with the whole body"
            )
        return self._finish({"kind": "patch", "path": lambda_path, "value": value})

    def abort(self, reason: AbortReason = "failed") -> Ref[None]:
        return self._finish({"kind": "abort", "reason": reason})

    def passthrough(self) -> Ref[None]:
        return self._finish({"kind": "passthrough"})

    def _describe(self) -> str:
        return f"{self._method} {self._pattern!r}{self._where}"

    def _finish(self, action: dict[str, Any]) -> Ref[None]:
        if self._recorded is not None:
            raise TraceError(
                f"net.route {self._describe()} already has its action; "
                "a route takes exactly one action, so start another s.net.route(...) for a second one"
            )
        if self._session.ctx.scope() is not self._scope:
            raise TraceError(
                f"net.route {self._describe()} is finished outside the block it was created in; "
                f"call {_ACTIONS} inside the same with block"
            )
        args: dict[str, Any] = {"method": self._method, "pattern": self._pattern, "action": action}
        if self._times is not None:
            args["times"] = self._times
        ref = self._session.record_op("net.route", args, at=self._at)
        self._recorded = ref
        return ref

    def _check_finished(self, _ctx: TraceContext) -> None:
        if self._recorded is None:
            raise TraceError(f"net.route {self._describe()} has no action; finish it with {_ACTIONS}")


CASSETTE_FLAG = "cassette"


class Net(Bound):
    __slots__ = ()

    def route(
        self, method: HttpMethod, pattern: Text, *, times: int | None = None, at: Time | None = None
    ) -> RouteBuilder:
        return RouteBuilder(self.session, method, pattern, times=times, at=at)

    def unroute(self, pattern: Text | None = None, *, at: Time | None = None) -> Ref[None]:
        args: dict[str, Any] = {}
        if pattern is not None:
            args["pattern"] = pattern
        return self.session.record_op("net.unroute", args, at=_pin(at))

    def shape(self, profile: str | NetProfile, *, at: Time | None = None) -> Ref[None]:
        if isinstance(profile, str):
            if not profile:
                raise ValueError("net.shape needs a network preset name such as '3g', or a NetProfile")
        elif not isinstance(profile, NetProfile):
            raise TypeError(
                f"net.shape expects a network preset name such as '3g' or a NetProfile, "
                f"got {type(profile).__name__} {profile!r}"
            )
        return self.session.record_op("net.shape", {"profile": profile}, at=_pin(at))

    @contextmanager
    def cassette(self, name: str, *, mode: CassetteMode = "auto") -> Iterator[Ref[None]]:
        if not isinstance(name, str):
            raise TypeError(f"net.cassette expects a literal name such as 'banka-arama', got {type(name).__name__} {name!r}")
        if self.session.ctx.has_flag(CASSETTE_FLAG):
            raise TraceError(
                f"s.net.cassette({name!r}) cannot be nested inside another cassette: both would record or replay the "
                "same requests; end the outer `with s.net.cassette(...)` block first"
            )
        with self.session.record_container("net.cassette", {"name": name, "mode": mode},
                                           flags=(CASSETTE_FLAG,)) as ref:
            yield ref


__all__ = ["CASSETTE_FLAG", "Net", "RouteBuilder"]
