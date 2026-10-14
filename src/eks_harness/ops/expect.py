from __future__ import annotations

import json
import re
import time
from typing import cast

from pydantic import BaseModel, JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    builtin_provenance,
)
from eks_harness.engine.values import truthy
from eks_harness.ir.ops.check import (
    Contains,
    Eq,
    ExpectValue,
    Falsy,
    Ge,
    Gt,
    Le,
    Length,
    Lt,
    Matches,
    Ne,
    Truthy,
)
from eks_harness.ir.time import TICKS_PER_SECOND, Time, format_time
from eks_harness.ops import BUILTIN_EXECUTOR_VERSION, EXPECT_EXECUTOR

VALUE_OP = "expect.value"
POLL_START_SECONDS = 0.25
POLL_MAX_SECONDS = 2.0
DEFAULT_EVENTUALLY_SECONDS = 10.0

_SHOWN_CHARS = 200


def _shown(value: JsonValue) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= _SHOWN_CHARS else text[:_SHOWN_CHARS] + "..."


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _same(left: JsonValue, right: JsonValue) -> bool:
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    return type(left) is type(right) and left == right


def _contains(value: JsonValue, item: JsonValue) -> str | None:
    if isinstance(value, str):
        if not isinstance(item, str):
            return f"expected the text {_shown(value)} to contain {_shown(item)}, which is not text"
        return None if item in value else f"expected {_shown(value)} to contain {_shown(item)}"
    if isinstance(value, list):
        if any(_same(entry, item) for entry in value):
            return None
        return f"expected the list {_shown(value)} to contain {_shown(item)}"
    if isinstance(value, dict):
        if not isinstance(item, str):
            return f"expected the object {_shown(value)} to have the key {_shown(item)}, which is not text"
        return None if item in value else f"expected the object {_shown(value)} to have the key {_shown(item)}"
    return f"expected a text, list or object containing {_shown(item)}, got {_shown(value)}"


def _matches(value: JsonValue, pattern: str) -> str | None:
    if not isinstance(value, str):
        return f"expected text matching /{pattern}/, got {_shown(value)}"
    try:
        compiled = re.compile(pattern)
    except re.error as problem:
        return f"the pattern /{pattern}/ is not a valid regular expression: {problem}"
    return None if compiled.search(value) else f"expected {_shown(value)} to match /{pattern}/"


def _ordered(kind: str, value: JsonValue, bound: JsonValue) -> str | None:
    if not _is_number(value):
        return f"expected a number {kind} {_shown(bound)}, got {_shown(value)}"
    if not _is_number(bound):
        return f"the bound {_shown(bound)} of {kind!r} is not a number"
    number, limit = cast(float, value), cast(float, bound)
    holds = {"gt": number > limit, "ge": number >= limit, "lt": number < limit, "le": number <= limit}[kind]
    words = {"gt": "greater than", "ge": "at least", "lt": "less than", "le": "at most"}[kind]
    return None if holds else f"expected a number {words} {_shown(bound)}, got {_shown(value)}"


def _length(assertion: Length, value: JsonValue) -> str | None:
    if not isinstance(value, str | list | dict):
        return f"expected a text, list or object to measure, got {_shown(value)}"
    size = len(value)
    if assertion.equals is not None and size != assertion.equals:
        return f"expected a length of {assertion.equals}, got {size}"
    if assertion.at_least is not None and size < assertion.at_least:
        return f"expected a length of at least {assertion.at_least}, got {size}"
    if assertion.at_most is not None and size > assertion.at_most:
        return f"expected a length of at most {assertion.at_most}, got {size}"
    return None


def assertion_failure(assertion: BaseModel, value: JsonValue) -> str | None:
    if isinstance(assertion, Eq):
        expected = cast(JsonValue, assertion.value)
        return None if _same(value, expected) else f"expected {_shown(expected)}, got {_shown(value)}"
    if isinstance(assertion, Ne):
        other = cast(JsonValue, assertion.value)
        return f"expected a value other than {_shown(other)}" if _same(value, other) else None
    if isinstance(assertion, Contains):
        return _contains(value, cast(JsonValue, assertion.value))
    if isinstance(assertion, Matches):
        return _matches(value, assertion.pattern)
    if isinstance(assertion, Gt | Ge | Lt | Le):
        return _ordered(assertion.kind, value, cast(JsonValue, assertion.value))
    if isinstance(assertion, Truthy):
        return None if truthy(value) else f"expected a truthy value, got {_shown(value)}"
    if isinstance(assertion, Falsy):
        return None if not truthy(value) else f"expected a falsy value, got {_shown(value)}"
    if isinstance(assertion, Length):
        return _length(assertion, value)
    raise NodeFailure("args_invalid", f"{VALUE_OP} has no assertion {type(assertion).__name__}")


def _seconds(duration: Time, what: str) -> float:
    if not duration.is_concrete:
        raise NodeFailure("args_invalid",
                          f"the {what} {format_time(duration)!r} of {VALUE_OP} must be a length in seconds such as "
                          "t('10s'), not one in frames, beats or bars")
    return duration.ticks / TICKS_PER_SECOND


class ExpectValueExecutor:
    def run(self, call: ExecContext) -> ExecResult:
        args = call.args
        if not isinstance(args, ExpectValue):
            raise NodeFailure("args_invalid", f"{call.op} needs expect.value arguments, got {type(args).__name__}")
        value = cast(JsonValue, args.value)
        failure = assertion_failure(args.assertion, value)
        if failure is not None and args.eventually:
            value, failure = self._poll(call, args, value, failure)
        if failure is not None:
            raise self._failed(args, value, failure)
        return ExecResult(value={"ok": True, "value": value})

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(EXPECT_EXECUTOR, BUILTIN_EXECUTOR_VERSION, ops=(VALUE_OP,), run=self.run,
                            provenance=builtin_provenance(),
                            description="Asserts a value, re-reading idempotent producers when eventually is set")

    def _window(self, call: ExecContext, args: ExpectValue) -> float:
        if args.timeout is not None:
            window = _seconds(args.timeout, "timeout")
        elif call.session is not None and call.session.timeout is not None:
            window = _seconds(call.session.timeout, "session timeout")
        else:
            window = DEFAULT_EVENTUALLY_SECONDS
        remaining = call.remaining()
        return window if remaining is None else min(window, remaining)

    def _poll(self, call: ExecContext, args: ExpectValue, value: JsonValue,
              failure: str) -> tuple[JsonValue, str | None]:
        window = self._window(call, args)
        started = time.monotonic()
        until = started + window
        delay = POLL_START_SECONDS
        reads = 0
        current: str | None = failure
        while current is not None:
            left = until - time.monotonic()
            if left <= 0:
                break
            if call.cancel.wait(min(delay, left)):
                call.check_cancelled()
            delay = min(delay * 2, POLL_MAX_SECONDS)
            try:
                value = call.reread(call.node.args["value"])
            except NodeFailure as problem:
                if problem.code != "assertion":
                    raise
                raise NodeFailure("assertion", f"{self._message(args, current)}; {problem.message}",
                                  detail={"value": value, "assertion": self._assertion(args), "reads": reads},
                                  soft=args.soft) from None
            reads += 1
            current = assertion_failure(args.assertion, value)
        if current is None:
            call.log(f"{VALUE_OP} held after {reads} re-reads", detail={"reads": reads})
            return value, None
        elapsed = time.monotonic() - started
        return value, f"{current} (still after {reads} re-reads in {elapsed:.1f}s)"

    def _message(self, args: ExpectValue, failure: str) -> str:
        return f"{args.message}: {failure}" if args.message else failure

    def _assertion(self, args: ExpectValue) -> JsonValue:
        return cast(JsonValue, args.assertion.model_dump(mode="json", by_alias=True))

    def _failed(self, args: ExpectValue, value: JsonValue, failure: str) -> NodeFailure:
        return NodeFailure("assertion", self._message(args, failure),
                           detail={"value": value, "assertion": self._assertion(args)}, soft=args.soft)


__all__ = [
    "DEFAULT_EVENTUALLY_SECONDS",
    "POLL_MAX_SECONDS",
    "POLL_START_SECONDS",
    "VALUE_OP",
    "ExpectValueExecutor",
    "assertion_failure",
]
