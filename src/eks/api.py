from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from eks.ref import Json, JsonLike, Ref
from eks.tracing import Bound, SessionCore
from eks.ui import Text
from eks_harness.ir.time import Time, ensure_time


def _statuses(value: int | Sequence[int] | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"expect_status expects an HTTP status code such as 200, got the bool {value!r}")
    if not isinstance(value, int) and (isinstance(value, str) or not isinstance(value, Sequence)):
        raise TypeError(
            "expect_status expects a status code or a sequence of them such as (200, 201), "
            f"got {type(value).__name__} {value!r}"
        )
    statuses = (value,) if isinstance(value, int) else tuple(value)
    if not statuses:
        raise ValueError("expect_status is empty; pass at least one status code, or leave it out")
    for status in statuses:
        if isinstance(status, bool) or not isinstance(status, int):
            raise TypeError(f"expect_status items must be int status codes, got {type(status).__name__} {status!r}")
        if not 100 <= status <= 599:
            raise ValueError(f"expect_status {status} is not an HTTP status code; codes run from 100 to 599")
    return statuses


def _persona(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} expects a persona name such as 'harman-admin', got {type(value).__name__} {value!r}")
    if not value:
        raise ValueError(f"{what} is empty; pass a persona name such as 'harman-admin', or leave it out")
    return value


class Api(Bound):
    __slots__ = ()

    def get(
        self, path: Text, *, query: Mapping[str, JsonLike] | None = None, headers: Mapping[str, Text] | None = None,
        as_: str | None = None, expect_status: int | Sequence[int] | None = None,
    ) -> Ref[Json]:
        return self._request("api.get", path, None, query, headers, as_, expect_status)

    def delete(
        self, path: Text, *, query: Mapping[str, JsonLike] | None = None, headers: Mapping[str, Text] | None = None,
        as_: str | None = None, expect_status: int | Sequence[int] | None = None,
    ) -> Ref[Json]:
        return self._request("api.delete", path, None, query, headers, as_, expect_status)

    def post(
        self, path: Text, *, json: JsonLike = None, query: Mapping[str, JsonLike] | None = None,
        headers: Mapping[str, Text] | None = None, as_: str | None = None,
        expect_status: int | Sequence[int] | None = None,
    ) -> Ref[Json]:
        return self._request("api.post", path, json, query, headers, as_, expect_status)

    def put(
        self, path: Text, *, json: JsonLike = None, query: Mapping[str, JsonLike] | None = None,
        headers: Mapping[str, Text] | None = None, as_: str | None = None,
        expect_status: int | Sequence[int] | None = None,
    ) -> Ref[Json]:
        return self._request("api.put", path, json, query, headers, as_, expect_status)

    def patch(
        self, path: Text, *, json: JsonLike = None, query: Mapping[str, JsonLike] | None = None,
        headers: Mapping[str, Text] | None = None, as_: str | None = None,
        expect_status: int | Sequence[int] | None = None,
    ) -> Ref[Json]:
        return self._request("api.patch", path, json, query, headers, as_, expect_status)

    def _request(
        self, op: str, path: Text, body: JsonLike, query: Mapping[str, JsonLike] | None, headers: Mapping[str, Text] | None,
        as_: str | None, expect_status: int | Sequence[int] | None,
    ) -> Ref[Json]:
        args: dict[str, Any] = {"path": path}
        if query is not None:
            args["query"] = dict(query)
        if headers is not None:
            args["headers"] = dict(headers)
        if as_ is not None:
            args["persona"] = _persona(as_, "as_")
        statuses = _statuses(expect_status)
        if statuses is not None:
            args["expect_status"] = statuses
        if body is not None:
            args["json"] = body
        return self.session.record_op(op, args)


class Backend(Bound):
    __slots__ = ()

    @property
    def info(self) -> Ref[Json]:
        return self.session.record_op("backend.info", {})


def act_as(session: SessionCore, persona: str, *, at: Time | None = None) -> Ref[None]:
    name = _persona(persona, "as_")
    return session.record_op("session.as", {"persona": name}, at=ensure_time(at, "at") if at is not None else None)


__all__ = ["Api", "Backend", "act_as"]
