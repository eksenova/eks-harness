from __future__ import annotations

import re
from typing import Any, overload

from eks.ref import (
    SENTINEL_CLOSE,
    SENTINEL_OPEN,
    Json,
    JsonLike,
    Ref,
    is_item,
    node_of,
    type_of,
)
from eks.tracing import (
    TraceContext,
    TraceError,
    active_session,
    current,
    current_session,
)
from eks.tracing import record as record_node
from eks.ui import TargetLike, Text, to_text
from eks_harness.ir.catalog.durations import ensure_positive_duration
from eks_harness.ir.ops.check import assertion_problem, literal_type
from eks_harness.ir.refs import REF_KEY
from eks_harness.ir.target import Target, ensure_target
from eks_harness.ir.time import Time


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def _carries_ref(text: str) -> bool:
    return SENTINEL_OPEN in text or SENTINEL_CLOSE in text


def _pattern(call: str, pattern: object, instead: str) -> str:
    if not isinstance(pattern, str):
        raise TypeError(f"{call} takes a pattern string, got {type(pattern).__name__}")
    if _carries_ref(pattern):
        raise TypeError(f"{call} must be plain text; refs cannot go into a pattern, use {instead}")
    try:
        re.compile(pattern)
    except re.error as error:
        raise TypeError(f"{call} {pattern!r} is not a valid regular expression: {error}") from None
    return pattern


def _text_assertion(
    kind: str, equals: Text | None, contains: Text | None, matches: str | None, **extra: Any
) -> dict[str, Any]:
    if equals is None and contains is None and matches is None:
        raise TypeError(
            f"expect(...).{kind}() needs at least one of equals, contains= or matches=; "
            f"for example .{kind}('Kaydet') or .{kind}(contains='Kayd')"
        )
    if matches is not None:
        _pattern(f"expect(...).{kind}() matches=", matches, "equals=ref or contains=ref")
    return _drop_none({
        "kind": kind,
        **extra,
        "equals": to_text(equals, f"expect(...).{kind}() equals") if equals is not None else None,
        "contains": to_text(contains, f"expect(...).{kind}() contains=") if contains is not None else None,
        "matches": matches,
    })


def _bounds(kind: str, equals: int | None, at_least: int | None, at_most: int | None) -> dict[str, Any]:
    if equals is None and at_least is None and at_most is None:
        raise TypeError(
            f"expect(...).{kind}() needs a bound: equals, at_least= or at_most=; for example .{kind}(at_least=1)"
        )
    for name, bound in (("equals", equals), ("at_least", at_least), ("at_most", at_most)):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
            raise TypeError(f"expect(...).{kind}() {name} takes an int, got {type(bound).__name__} {bound!r}")
    if at_least is not None and at_most is not None and at_least > at_most:
        raise TypeError(
            f"expect(...).{kind}() at_least={at_least} is above at_most={at_most}, so it can never pass; "
            "swap or fix the bounds"
        )
    if equals is not None and (
        (at_least is not None and equals < at_least) or (at_most is not None and equals > at_most)
    ):
        raise TypeError(
            f"expect(...).{kind}() equals {equals} lies outside at_least={at_least}, at_most={at_most}, "
            "so it can never pass; drop the bounds or fix equals"
        )
    return _drop_none({"kind": kind, "equals": equals, "at_least": at_least, "at_most": at_most})


class _Options:
    __slots__ = ("_eventually", "_soft", "_timeout")

    def __init__(self, *, soft: bool, eventually: bool, timeout: Time | None) -> None:
        self._soft = soft
        self._eventually = eventually
        self._timeout = timeout

    def _options(self, message: str | None) -> dict[str, Any]:
        if message is not None and not isinstance(message, str):
            raise TypeError(f"expect message= takes a plain string, got {type(message).__name__}")
        if message is not None and _carries_ref(message):
            raise TypeError(
                "expect message= must be plain text; refs cannot go into a message, the run report already "
                "shows the compared values"
            )
        timeout = ensure_positive_duration(self._timeout, "expect timeout") if self._timeout is not None else None
        return _drop_none({
            "soft": self._soft,
            "eventually": self._eventually,
            "timeout": timeout,
            "message": message,
        })


class TargetExpectation(_Options):
    __slots__ = ("_target",)

    def __init__(
        self, target: TargetLike, *, soft: bool = False, eventually: bool = False, timeout: Time | None = None
    ) -> None:
        super().__init__(soft=soft, eventually=eventually, timeout=timeout)
        self._target = target

    def _record(self, assertion: dict[str, Any], message: str | None) -> Ref[Json]:
        session = current_session()
        target = ensure_target(self._target, "expect target", session.ctx.registry.target_kinds())
        args = {"target": target, "assertion": assertion, **self._options(message)}
        return session.record_op("expect.target", args)

    def visible(self, *, message: str | None = None) -> Ref[Json]:
        return self._record({"kind": "visible"}, message)

    def hidden(self, *, message: str | None = None) -> Ref[Json]:
        return self._record({"kind": "hidden"}, message)

    def enabled(self, *, message: str | None = None) -> Ref[Json]:
        return self._record({"kind": "enabled"}, message)

    def disabled(self, *, message: str | None = None) -> Ref[Json]:
        return self._record({"kind": "disabled"}, message)

    def checked(self, on: bool = True, *, message: str | None = None) -> Ref[Json]:
        if not isinstance(on, bool):
            raise TypeError(f"expect(...).checked() takes True or False, got {type(on).__name__} {on!r}")
        return self._record({"kind": "checked", "on": on}, message)

    def text(
        self, equals: Text | None = None, *, contains: Text | None = None, matches: str | None = None,
        message: str | None = None,
    ) -> Ref[Json]:
        return self._record(_text_assertion("text", equals, contains, matches), message)

    def value(
        self, equals: Text | None = None, *, contains: Text | None = None, matches: str | None = None,
        message: str | None = None,
    ) -> Ref[Json]:
        return self._record(_text_assertion("value", equals, contains, matches), message)

    def count(
        self, equals: int | None = None, *, at_least: int | None = None, at_most: int | None = None,
        message: str | None = None,
    ) -> Ref[Json]:
        return self._record(_bounds("count", equals, at_least, at_most), message)

    def attr(
        self, name: str, equals: Text | None = None, *, contains: Text | None = None, matches: str | None = None,
        message: str | None = None,
    ) -> Ref[Json]:
        if not isinstance(name, str) or not name:
            raise TypeError(f"expect(...).attr() takes the attribute name first, got {name!r}")
        return self._record(_text_assertion("attr", equals, contains, matches, name=name), message)


class ValueExpectation(_Options):
    __slots__ = ("_value",)

    def __init__(
        self, value: Ref[Any], *, soft: bool = False, eventually: bool = False, timeout: Time | None = None
    ) -> None:
        super().__init__(soft=soft, eventually=eventually, timeout=timeout)
        self._value = value

    def _require_rereadable(self, ctx: TraceContext) -> None:
        if is_item(self._value):
            raise TraceError(self._item_problem(ctx))
        producer = ctx.node(node_of(self._value))
        if not ctx.registry.op(producer.op).idempotent:
            raise TraceError(
                f"expect.eventually(ref) re-reads the op that produced the ref, but {producer.op!r} is not "
                "idempotent and cannot run again; assert on a read such as s.ui.text(...), s.ui.count(...) or "
                "s.ui.state(...), or use plain expect(ref) to check the value once"
            )

    def _item_problem(self, ctx: TraceContext) -> str:
        try:
            loop = ctx.node(node_of(self._value))
        except TraceError:
            return (
                "expect.eventually(item) cannot re-read the item of a literal eks.each: it is a constant written "
                "into the plan, so use plain expect(item) to check it"
            )
        over = loop.args.get("over")
        source = over.get(REF_KEY) if isinstance(over, dict) else None
        origin = f" from {ctx.node(source).op!r}" if isinstance(source, str) else ""
        return (
            "expect.eventually(item) cannot re-read the item of eks.each: the loop takes a snapshot of its list"
            f"{origin} before the first iteration, so the item never changes; put expect.eventually on the read "
            "that produced the list (or on a fresh read inside the loop), or use plain expect(item) to check the "
            "item once"
        )

    def _check_types(self, assertion: dict[str, Any]) -> None:
        compared = assertion.get("value")
        compared_type = type_of(compared) if isinstance(compared, Ref) else literal_type(compared)
        problem = assertion_problem(assertion["kind"], type_of(self._value), compared_type)
        if problem is not None:
            raise TypeError(f"expect(ref).{assertion['kind']}(): {problem}")

    def _record(self, assertion: dict[str, Any], message: str | None) -> Ref[Json]:
        self._check_types(assertion)
        args = {"value": self._value, "assertion": assertion, **self._options(message)}
        session = active_session()
        if self._eventually:
            self._require_rereadable(session.ctx if session is not None else current())
        if session is not None:
            return session.record_op("expect.value", args)
        return record_node("expect.value", args)

    def _compare(self, kind: str, value: Any, message: str | None) -> Ref[Json]:
        if value is None:
            raise TypeError(
                f"expect(ref).{kind}(None) compares with nothing; use .falsy() for an empty value or compare with "
                "a concrete value"
            )
        return self._record({"kind": kind, "value": value}, message)

    def eq(self, value: JsonLike, *, message: str | None = None) -> Ref[Json]:
        return self._compare("eq", value, message)

    def ne(self, value: JsonLike, *, message: str | None = None) -> Ref[Json]:
        return self._compare("ne", value, message)

    def contains(self, value: JsonLike, *, message: str | None = None) -> Ref[Json]:
        return self._compare("contains", value, message)

    def matches(self, pattern: str, *, message: str | None = None) -> Ref[Json]:
        checked = _pattern("expect(ref).matches()", pattern, ".eq(ref) or .contains(ref)")
        return self._record({"kind": "matches", "pattern": checked}, message)

    def gt(self, value: JsonLike, *, message: str | None = None) -> Ref[Json]:
        return self._compare("gt", value, message)

    def ge(self, value: JsonLike, *, message: str | None = None) -> Ref[Json]:
        return self._compare("ge", value, message)

    def lt(self, value: JsonLike, *, message: str | None = None) -> Ref[Json]:
        return self._compare("lt", value, message)

    def le(self, value: JsonLike, *, message: str | None = None) -> Ref[Json]:
        return self._compare("le", value, message)

    def truthy(self, *, message: str | None = None) -> Ref[Json]:
        return self._record({"kind": "truthy"}, message)

    def falsy(self, *, message: str | None = None) -> Ref[Json]:
        return self._record({"kind": "falsy"}, message)

    def length(
        self, equals: int | None = None, *, at_least: int | None = None, at_most: int | None = None,
        message: str | None = None,
    ) -> Ref[Json]:
        return self._record(_bounds("length", equals, at_least, at_most), message)


def _subject(
    subject: Ref[Any] | TargetLike, *, soft: bool, eventually: bool, timeout: Time | None
) -> ValueExpectation | TargetExpectation:
    if isinstance(subject, Ref):
        return ValueExpectation(subject, soft=soft, eventually=eventually, timeout=timeout)
    if isinstance(subject, Target | str):
        return TargetExpectation(subject, soft=soft, eventually=eventually, timeout=timeout)
    raise TypeError(
        f"expect() takes a target (by.text('Kaydet'), '#save') or a Ref produced earlier, got "
        f"{type(subject).__name__} {subject!r}"
    )


def _wait_timeout(subject: Ref[Any] | TargetLike, timeout: Time | None, call: str) -> Time | None:
    if timeout is None:
        return None
    if isinstance(subject, Ref):
        raise TypeError(
            f"{call}(ref, timeout=...) has nothing to wait for: a plain value expectation checks the value once; "
            "use expect.eventually(ref, timeout=...) to re-read an idempotent producer until it passes"
        )
    return ensure_positive_duration(timeout, f"{call} timeout")


class Expect:
    @overload
    def __call__(self, subject: Ref[Any]) -> ValueExpectation: ...

    @overload
    def __call__(self, subject: TargetLike, *, timeout: Time | None = None) -> TargetExpectation: ...

    def __call__(
        self, subject: Ref[Any] | TargetLike, *, timeout: Time | None = None
    ) -> ValueExpectation | TargetExpectation:
        checked = _wait_timeout(subject, timeout, "expect")
        return _subject(subject, soft=False, eventually=False, timeout=checked)

    @overload
    def soft(self, subject: Ref[Any]) -> ValueExpectation: ...

    @overload
    def soft(self, subject: TargetLike, *, timeout: Time | None = None) -> TargetExpectation: ...

    def soft(
        self, subject: Ref[Any] | TargetLike, *, timeout: Time | None = None
    ) -> ValueExpectation | TargetExpectation:
        checked = _wait_timeout(subject, timeout, "expect.soft")
        return _subject(subject, soft=True, eventually=False, timeout=checked)

    @overload
    def eventually(self, subject: Ref[Any], *, timeout: Time | None = None) -> ValueExpectation: ...

    @overload
    def eventually(self, subject: TargetLike, *, timeout: Time | None = None) -> TargetExpectation: ...

    def eventually(
        self, subject: Ref[Any] | TargetLike, *, timeout: Time | None = None
    ) -> ValueExpectation | TargetExpectation:
        checked = ensure_positive_duration(timeout, "expect.eventually timeout") if timeout is not None else None
        return _subject(subject, soft=False, eventually=True, timeout=checked)


expect = Expect()

__all__ = ["Expect", "TargetExpectation", "ValueExpectation", "expect"]
