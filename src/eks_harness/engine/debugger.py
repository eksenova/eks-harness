from __future__ import annotations

import ast
import functools
import re
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Literal, cast

from pydantic import JsonValue

from eks_harness.engine.model import (
    Breakpoint,
    ControlRequest,
    ControlResponse,
    DebuggerState,
    NodeError,
    PauseInfo,
    PauseReason,
    RunStatus,
)
from eks_harness.ir.plan import NODE_ID_PATTERN

CONDITION_NAMES = ("node", "op", "label", "src", "attempt", "instance", "args", "values", "error")
CONDITION_FUNCTIONS = ("len", "contains", "startswith", "endswith", "matches", "lower", "upper")
CONDITION_MAX_LENGTH = 1024
CONDITION_MAX_DEPTH = 32
IMMEDIATE_ACTIONS = frozenset({"status", "break_list", "break_add", "break_remove", "pause"})
PAUSED_ACTIONS = frozenset({"continue", "step", "retry", "skip", "edit", "do"})
ResumeMode = Literal["continue", "step"]


class ConditionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BreakpointSpec:
    text: str
    node: str | None = None
    op: str | None = None
    label: str | None = None
    condition: str | None = None


_NODE_ID = re.compile(NODE_ID_PATTERN)
_WHEN = "when:"
_PREFIXES = ("node:", "op:", "label:")


def _spec_error(text: str, problem: str) -> ValueError:
    return ValueError(
        f"invalid breakpoint spec {text!r}: {problem}; write node:ID, ID, op:GLOB, label:TEXT or when:EXPR "
        "joined by commas, with when: last"
    )


def parse_breakpoint(text: str) -> BreakpointSpec:
    found: dict[str, str] = {}
    rest = text.strip()
    if not rest:
        raise _spec_error(text, "the spec is empty")
    while rest:
        if rest.startswith(_WHEN):
            expression = rest[len(_WHEN):].strip()
            try:
                Condition(expression)
            except ConditionError as error:
                raise _spec_error(text, str(error)) from error
            found["condition"] = expression
            break
        criterion, separator, rest = rest.partition(",")
        criterion = criterion.strip()
        rest = rest.strip()
        if not criterion:
            raise _spec_error(text, "it has an empty criterion")
        name, value = _criterion(text, criterion)
        if name in found:
            raise _spec_error(text, f"it names {name} twice")
        found[name] = value
        if not rest and separator:
            raise _spec_error(text, "it ends with a comma")
    return BreakpointSpec(text=text, node=found.get("node"), op=found.get("op"), label=found.get("label"),
                          condition=found.get("condition"))


def _criterion(text: str, criterion: str) -> tuple[str, str]:
    for prefix in _PREFIXES:
        if criterion.startswith(prefix):
            value = criterion[len(prefix):].strip()
            name = prefix[:-1]
            if not value:
                raise _spec_error(text, f"{prefix} needs a value")
            if name == "node" and not _NODE_ID.fullmatch(value):
                raise _spec_error(text, f"{value!r} is not a node id such as n0000000012")
            return name, value
    if _NODE_ID.fullmatch(criterion):
        return "node", criterion
    raise _spec_error(text, f"unknown criterion {criterion!r}")


_COMPARE_OPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.In: "in",
    ast.NotIn: "not in",
}
_BINARY_OPS: dict[type[ast.operator], str] = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Mod: "%"}
_ARITY = {"len": 1, "lower": 1, "upper": 1, "contains": 2, "startswith": 2, "endswith": 2, "matches": 2}
_CONSTRUCTS: dict[type[ast.AST], str] = {
    ast.Lambda: "a lambda",
    ast.ListComp: "a comprehension",
    ast.SetComp: "a comprehension",
    ast.DictComp: "a comprehension",
    ast.GeneratorExp: "a comprehension",
    ast.Starred: "a starred expression",
    ast.JoinedStr: "an f-string",
    ast.FormattedValue: "an f-string",
    ast.IfExp: "a conditional expression",
    ast.Dict: "a dict display",
    ast.Set: "a set display",
    ast.NamedExpr: "an assignment expression",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield",
    ast.Slice: "a slice",
}


def _construct(node: ast.AST) -> str:
    return _CONSTRUCTS.get(type(node), f"{type(node).__name__} expressions")


class Condition:
    def __init__(self, text: str) -> None:
        self.text = text
        if len(text) > CONDITION_MAX_LENGTH:
            raise ConditionError(f"the condition is longer than {CONDITION_MAX_LENGTH} characters")
        if not text.strip():
            raise ConditionError("the condition is empty")
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except SyntaxError as error:
            raise ConditionError(f"the condition {text!r} is not a valid expression: {error.msg}") from error
        self._body = tree.body
        _check(self._body, 1)

    def evaluate(self, scope: Mapping[str, JsonValue]) -> bool:
        try:
            return bool(_evaluate(self._body, scope))
        except ConditionError:
            raise
        except (TypeError, ValueError, ZeroDivisionError, OverflowError, RecursionError) as error:
            raise ConditionError(f"the condition {self.text!r} failed: {error}") from error


def _refuse(node: ast.AST) -> ConditionError:
    return ConditionError(f"{_construct(node)} is not allowed in a condition")


def _check(node: ast.AST, depth: int) -> None:
    if depth > CONDITION_MAX_DEPTH:
        raise ConditionError(f"the condition nests deeper than {CONDITION_MAX_DEPTH} levels")
    match node:
        case ast.Constant(value=value):
            if value is not None and not isinstance(value, bool | int | float | str):
                raise ConditionError(f"the constant {value!r} is not allowed in a condition")
        case ast.Name(id=name):
            if name not in CONDITION_NAMES:
                raise ConditionError(f"unknown name {name!r} in a condition; use one of {', '.join(CONDITION_NAMES)}")
        case ast.Attribute(value=value):
            _check(value, depth + 1)
        case ast.Subscript(value=value, slice=key):
            _check(value, depth + 1)
            if _constant_key(key) is None:
                raise ConditionError("a subscript in a condition takes a constant string key or integer index")
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            for op in ops:
                if type(op) not in _COMPARE_OPS:
                    raise ConditionError(f"the comparison {type(op).__name__} is not allowed in a condition")
            for operand in (left, *comparators):
                _check(operand, depth + 1)
        case ast.BoolOp(values=operands):
            for operand in operands:
                _check(operand, depth + 1)
        case ast.UnaryOp(op=op, operand=operand):
            if not isinstance(op, ast.Not | ast.USub):
                raise ConditionError(f"the unary operator {type(op).__name__} is not allowed in a condition")
            _check(operand, depth + 1)
        case ast.BinOp(left=left, op=op, right=right):
            if type(op) not in _BINARY_OPS:
                raise ConditionError(f"the operator {type(op).__name__} is not allowed in a condition")
            _check(left, depth + 1)
            _check(right, depth + 1)
        case ast.List(elts=elements) | ast.Tuple(elts=elements):
            for element in elements:
                _check(element, depth + 1)
        case ast.Call(func=func, args=arguments, keywords=keywords):
            _check_call(func, arguments, keywords, depth)
        case _:
            raise _refuse(node)


def _check_call(func: ast.expr, arguments: list[ast.expr], keywords: list[ast.keyword], depth: int) -> None:
    if not isinstance(func, ast.Name) or func.id not in CONDITION_FUNCTIONS:
        called = func.id if isinstance(func, ast.Name) else ast.unparse(func)
        raise ConditionError(
            f"calling {called!r} is not allowed in a condition; the functions are {', '.join(CONDITION_FUNCTIONS)}"
        )
    if keywords:
        raise ConditionError(f"keyword arguments are not allowed in a condition ({func.id})")
    if len(arguments) != _ARITY[func.id]:
        raise ConditionError(f"{func.id} takes {_ARITY[func.id]} argument(s), got {len(arguments)}")
    for argument in arguments:
        _check(argument, depth + 1)
    if func.id == "matches":
        pattern = arguments[1]
        if not (isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)):
            raise ConditionError("matches takes a constant pattern string as its second argument")
        _compile_pattern(pattern.value)


def _constant_key(node: ast.expr) -> str | int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str | int) and not isinstance(node.value, bool):
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, int) and not isinstance(node.operand.value, bool)):
        return -node.operand.value
    return None


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    return f"a {type(value).__name__}"


def _lookup(container: object, key: str | int) -> object:
    if container is None:
        return None
    if isinstance(container, dict):
        return cast("dict[object, object]", container).get(key) if isinstance(key, str) else None
    if isinstance(container, list | tuple):
        items = cast("Sequence[object]", container)
        if isinstance(key, str):
            raise ConditionError(f"cannot read the key {key!r} of a list")
        return items[key] if -len(items) <= key < len(items) else None
    raise ConditionError(f"cannot read {key!r} of {_kind(container)}")


def _number(value: object, operator: str) -> int | float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    raise ConditionError(f"{operator} needs numbers, got {_kind(value)}")


def _binary(op: ast.operator, left: object, right: object) -> object:
    if isinstance(op, ast.Add):
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, list) and isinstance(right, list):
            return [*cast("list[object]", left), *cast("list[object]", right)]
        return _number(left, "+") + _number(right, "+")
    symbol = _BINARY_OPS[type(op)]
    a = _number(left, symbol)
    b = _number(right, symbol)
    if isinstance(op, ast.Sub):
        return a - b
    if isinstance(op, ast.Mult):
        return a * b
    if b == 0:
        raise ConditionError(f"{symbol} by zero")
    return a / b if isinstance(op, ast.Div) else a % b


def _compare(op: ast.cmpop, left: object, right: object) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.In | ast.NotIn):
        found = _contains(right, left)
        return found if isinstance(op, ast.In) else not found
    ordered = (int, float, str)
    if not (isinstance(left, ordered) and isinstance(right, ordered)) or isinstance(left, str) != isinstance(right, str):
        raise ConditionError(f"cannot compare {_kind(left)} with {_kind(right)} using {_COMPARE_OPS[type(op)]}")
    a = cast("float", left)
    b = cast("float", right)
    if isinstance(op, ast.Lt):
        return a < b
    if isinstance(op, ast.LtE):
        return a <= b
    if isinstance(op, ast.Gt):
        return a > b
    return a >= b


def _contains(container: object, item: object) -> bool:
    if container is None:
        return False
    if isinstance(container, str):
        if not isinstance(item, str):
            raise ConditionError(f"cannot look for {_kind(item)} in a str")
        return item in container
    if isinstance(container, list | tuple):
        return item in cast("Sequence[object]", container)
    if isinstance(container, dict):
        return isinstance(item, str) and item in cast("dict[str, object]", container)
    raise ConditionError(f"cannot look for a value in {_kind(container)}")


def _text(value: object, function: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ConditionError(f"{function} needs a str, got {_kind(value)}")


def _call(name: str, arguments: list[object]) -> object:
    if name == "len":
        (value,) = arguments
        if isinstance(value, str | list | dict | tuple):
            return len(cast("Sequence[object]", value))
        raise ConditionError(f"len needs a str, a list or an object, got {_kind(value)}")
    if name == "contains":
        return _contains(arguments[0], arguments[1])
    if name in ("lower", "upper"):
        value = _text(arguments[0], name)
        if value is None:
            return None
        return value.lower() if name == "lower" else value.upper()
    text = _text(arguments[0], name)
    other = arguments[1]
    if not isinstance(other, str):
        raise ConditionError(f"{name} needs a str as its second argument, got {_kind(other)}")
    if text is None:
        return False
    if name == "startswith":
        return text.startswith(other)
    if name == "endswith":
        return text.endswith(other)
    return _search(_compile_pattern(other), text)


_PATTERN_PROGRAM_LIMIT = 512
_PATTERN_MAX_DEPTH = 32
_MATCH_STEP_LIMIT = 1_000_000
_CATEGORIES = frozenset("dDwWsS")
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v", "a": "\a"}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_QUANTIFIER = re.compile(r"\{(\d*)(,?)(\d*)\}")
_OCTAL = "01234567"
_GROUP_REFUSALS = (
    ("(?=", "a lookahead"),
    ("(?!", "a negative lookahead"),
    ("(?<=", "a lookbehind"),
    ("(?<!", "a negative lookbehind"),
    ("(?>", "an atomic group"),
    ("(?P=", "a backreference"),
    ("(?(", "a conditional group"),
    ("(?#", "a comment group"),
)
AssertionKind = Literal["start", "end", "eol", "word", "nonword"]


def _in_category(category: str, char: str) -> bool:
    lowered = category.lower()
    if lowered == "d":
        found = char.isdecimal()
    elif lowered == "w":
        found = char.isalnum() or char == "_"
    else:
        found = char.isspace()
    return found if category == lowered else not found


def _is_word(char: str) -> bool:
    return char.isalnum() or char == "_"


@dataclass(frozen=True, slots=True)
class _CharClass:
    ranges: tuple[tuple[int, int], ...] = ()
    categories: tuple[str, ...] = ()
    negated: bool = False

    def test(self, char: str) -> bool:
        code = ord(char)
        found = any(low <= code <= high for low, high in self.ranges) or any(
            _in_category(category, char) for category in self.categories
        )
        return found != self.negated


@dataclass(frozen=True, slots=True)
class _Assert:
    kind: AssertionKind


@dataclass(frozen=True, slots=True)
class _Concat:
    items: tuple[_PatternNode, ...]


@dataclass(frozen=True, slots=True)
class _Alternation:
    options: tuple[_PatternNode, ...]


@dataclass(frozen=True, slots=True)
class _Repeat:
    body: _PatternNode
    low: int
    high: int | None


_PatternNode = _CharClass | _Assert | _Concat | _Alternation | _Repeat


@dataclass(slots=True)
class _Instruction:
    kind: Literal["char", "split", "jump", "assert", "match"]
    test: _CharClass | None = None
    first: int = 0
    second: int = 0
    assertion: AssertionKind = "start"


class _PatternParser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.position = 0
        self.depth = 0

    def error(self, problem: str) -> ConditionError:
        return ConditionError(
            f"the pattern {self.pattern!r} cannot be used in matches: {problem} at position {self.position}"
        )

    def peek(self) -> str | None:
        return self.pattern[self.position] if self.position < len(self.pattern) else None

    def parse(self) -> _PatternNode:
        node = self.alternation()
        if self.position < len(self.pattern):
            raise self.error("unbalanced parenthesis")
        return node

    def alternation(self) -> _PatternNode:
        options = [self.concatenation()]
        while self.peek() == "|":
            self.position += 1
            options.append(self.concatenation())
        return options[0] if len(options) == 1 else _Alternation(tuple(options))

    def concatenation(self) -> _PatternNode:
        items: list[_PatternNode] = []
        while self.peek() not in (None, "|", ")"):
            items.append(self.repeat())
        return items[0] if len(items) == 1 else _Concat(tuple(items))

    def quantifier(self) -> tuple[int, int | None, int] | None:
        char = self.peek()
        if char == "*":
            return 0, None, 1
        if char == "+":
            return 1, None, 1
        if char == "?":
            return 0, 1, 1
        if char != "{":
            return None
        found = _QUANTIFIER.match(self.pattern, self.position)
        if found is None or found.group(0) == "{}":
            return None
        low_text, comma, high_text = found.groups()
        low = int(low_text) if low_text else 0
        high = (int(high_text) if high_text else None) if comma else low
        for count in (low, high):
            if count is not None and count > _PATTERN_PROGRAM_LIMIT:
                raise self.error(f"the repeat count {count} is over {_PATTERN_PROGRAM_LIMIT}")
        if high is not None and low > high:
            raise self.error("the minimum repeat is greater than the maximum")
        return low, high, len(found.group(0))

    def repeat(self) -> _PatternNode:
        if self.quantifier() is not None:
            raise self.error("nothing to repeat")
        node = self.atom()
        found = self.quantifier()
        if found is None:
            return node
        if isinstance(node, _Assert):
            raise self.error("nothing to repeat")
        low, high, width = found
        self.position += width
        following = self.peek()
        if following == "+":
            raise self.error("a possessive quantifier is not supported")
        if following == "?":
            self.position += 1
        if self.quantifier() is not None:
            raise self.error("multiple repeat")
        return _Repeat(node, low, high)

    def atom(self) -> _PatternNode:
        char = self.pattern[self.position]
        if char == "(":
            return self.group()
        if char == ")":
            raise self.error("unbalanced parenthesis")
        if char == "[":
            return self.char_class()
        self.position += 1
        if char == ".":
            return _CharClass(ranges=((10, 10),), negated=True)
        if char == "^":
            return _Assert("start")
        if char == "$":
            return _Assert("eol")
        if char == "\\":
            self.position -= 1
            return self.escape(in_class=False)
        return _CharClass(ranges=((ord(char), ord(char)),))

    def group(self) -> _PatternNode:
        rest = self.pattern[self.position:]
        if rest.startswith("(?:"):
            self.position += 3
        elif rest.startswith("(?P<"):
            end = rest.find(">")
            name = rest[4:end] if end > 0 else ""
            if not name.isidentifier():
                raise self.error("a group name must be an identifier closed by >")
            self.position += end + 1
        elif rest.startswith("(?"):
            construct = next((label for prefix, label in _GROUP_REFUSALS if rest.startswith(prefix)), "an inline flag")
            raise self.error(f"{construct} is not supported (use lower() for case-insensitive matching)"
                             if construct == "an inline flag" else f"{construct} is not supported")
        else:
            self.position += 1
        self.depth += 1
        if self.depth > _PATTERN_MAX_DEPTH:
            raise self.error(f"groups nest deeper than {_PATTERN_MAX_DEPTH} levels")
        body = self.alternation()
        if self.peek() != ")":
            raise self.error("missing ), unterminated subpattern")
        self.position += 1
        self.depth -= 1
        return _Concat((body,))

    def escape(self, *, in_class: bool) -> _CharClass | _Assert:
        self.position += 1
        char = self.peek()
        if char is None:
            raise self.error("bad escape (end of pattern)")
        self.position += 1
        if char in _CATEGORIES:
            return _CharClass(categories=(char,))
        if not in_class and char in "bBAZ":
            kinds: dict[str, AssertionKind] = {"b": "word", "B": "nonword", "A": "start", "Z": "end"}
            return _Assert(kinds[char])
        if in_class and char == "b":
            return _CharClass(ranges=((8, 8),))
        if char in _SIMPLE_ESCAPES:
            code = ord(_SIMPLE_ESCAPES[char])
            return _CharClass(ranges=((code, code),))
        if char in _HEX_ESCAPES:
            return self.code_point(self.hex_digits(_HEX_ESCAPES[char]))
        if char == "N":
            return self.named_char()
        if char in _OCTAL:
            return self.octal(char)
        if char.isdigit():
            raise self.error("a backreference is not supported" if not in_class else f"bad escape \\{char}")
        if char.isascii() and char.isalnum():
            raise self.error(f"bad escape \\{char}")
        return _CharClass(ranges=((ord(char), ord(char)),))

    def hex_digits(self, count: int) -> int:
        digits = self.pattern[self.position:self.position + count]
        if len(digits) != count or any(digit not in "0123456789abcdefABCDEF" for digit in digits):
            raise self.error(f"an escape needs {count} hex digits")
        self.position += count
        return int(digits, 16)

    def code_point(self, code: int) -> _CharClass:
        if code > 0x10FFFF:
            raise self.error("the escape is not a valid code point")
        return _CharClass(ranges=((code, code),))

    def named_char(self) -> _CharClass:
        if self.peek() != "{":
            raise self.error("\\N needs a name in braces")
        end = self.pattern.find("}", self.position)
        if end < 0:
            raise self.error("\\N needs a name in braces")
        name = self.pattern[self.position + 1:end]
        try:
            char = unicodedata.lookup(name)
        except KeyError as error:
            raise self.error(f"unknown character name {name!r}") from error
        self.position = end + 1
        return _CharClass(ranges=((ord(char), ord(char)),))

    def octal(self, first: str) -> _CharClass:
        if first == "0":
            digits = first
            while len(digits) < 3 and self.peek() in tuple(_OCTAL):
                digits += self.pattern[self.position]
                self.position += 1
            return self.code_point(int(digits, 8))
        following = self.pattern[self.position:self.position + 2]
        if len(following) == 2 and all(digit in _OCTAL for digit in following):
            self.position += 2
            code = int(first + following, 8)
            if code > 0o377:
                raise self.error("the octal escape is over \\377")
            return self.code_point(code)
        raise self.error("a backreference is not supported")

    def class_member(self) -> _CharClass:
        if self.peek() == "\\":
            found = self.escape(in_class=True)
            if isinstance(found, _Assert):
                raise self.error("bad escape in a character set")
            return found
        char = self.pattern[self.position]
        self.position += 1
        return _CharClass(ranges=((ord(char), ord(char)),))

    def char_class(self) -> _CharClass:
        self.position += 1
        negated = self.peek() == "^"
        if negated:
            self.position += 1
        ranges: list[tuple[int, int]] = []
        categories: list[str] = []
        first = True
        while True:
            char = self.peek()
            if char is None:
                raise self.error("unterminated character set")
            if char == "]" and not first:
                self.position += 1
                break
            first = False
            member = self.class_member()
            is_range = (self.peek() == "-" and self.position + 1 < len(self.pattern)
                        and self.pattern[self.position + 1] != "]")
            if not is_range:
                ranges.extend(member.ranges)
                categories.extend(member.categories)
                continue
            self.position += 1
            upper = self.class_member()
            if member.categories or upper.categories:
                raise self.error("bad character range")
            low = member.ranges[0][0]
            high = upper.ranges[0][0]
            if low > high:
                raise self.error("bad character range")
            ranges.append((low, high))
        return _CharClass(ranges=tuple(ranges), categories=tuple(categories), negated=negated)


class _ProgramBuilder:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.program: list[_Instruction] = []

    def add(self, instruction: _Instruction) -> int:
        if len(self.program) >= _PATTERN_PROGRAM_LIMIT:
            raise ConditionError(
                f"the pattern {self.pattern!r} cannot be used in matches: it expands to more than "
                f"{_PATTERN_PROGRAM_LIMIT} steps"
            )
        self.program.append(instruction)
        return len(self.program) - 1

    def emit(self, node: _PatternNode) -> None:
        match node:
            case _CharClass():
                self.add(_Instruction("char", test=node))
            case _Assert(kind=kind):
                self.add(_Instruction("assert", assertion=kind))
            case _Concat(items=items):
                for item in items:
                    self.emit(item)
            case _Alternation(options=options):
                self.alternation(options)
            case _Repeat(body=body, low=low, high=high):
                self.repeat(body, low, high)

    def alternation(self, options: tuple[_PatternNode, ...]) -> None:
        jumps: list[int] = []
        for option in options[:-1]:
            split = self.add(_Instruction("split"))
            self.program[split].first = split + 1
            self.emit(option)
            jumps.append(self.add(_Instruction("jump")))
            self.program[split].second = len(self.program)
        self.emit(options[-1])
        for jump in jumps:
            self.program[jump].first = len(self.program)

    def repeat(self, body: _PatternNode, low: int, high: int | None) -> None:
        for _ in range(low):
            self.emit(body)
        if high is None:
            split = self.add(_Instruction("split"))
            self.program[split].first = split + 1
            self.emit(body)
            jump = self.add(_Instruction("jump"))
            self.program[jump].first = split
            self.program[split].second = len(self.program)
            return
        splits: list[int] = []
        for _ in range(high - low):
            split = self.add(_Instruction("split"))
            self.program[split].first = split + 1
            splits.append(split)
            self.emit(body)
        for split in splits:
            self.program[split].second = len(self.program)


@functools.lru_cache(maxsize=256)
def _compile_pattern(pattern: str) -> tuple[_Instruction, ...]:
    builder = _ProgramBuilder(pattern)
    builder.emit(_PatternParser(pattern).parse())
    builder.add(_Instruction("match"))
    return tuple(builder.program)


def _holds(kind: AssertionKind, text: str, index: int) -> bool:
    size = len(text)
    if kind == "start":
        return index == 0
    if kind == "end":
        return index == size
    if kind == "eol":
        return index == size or (index == size - 1 and text[index] == "\n")
    left = index > 0 and _is_word(text[index - 1])
    right = index < size and _is_word(text[index])
    return (left != right) if kind == "word" else (left == right)


def _search(program: tuple[_Instruction, ...], text: str) -> bool:
    steps = 0
    seeds: list[int] = []
    for index in range(len(text) + 1):
        seeds.append(0)
        visited: set[int] = set()
        waiting: list[int] = []
        stack = list(reversed(seeds))
        while stack:
            pc = stack.pop()
            if pc in visited:
                continue
            visited.add(pc)
            steps += 1
            if steps > _MATCH_STEP_LIMIT:
                raise ConditionError(
                    f"matches gave up after {_MATCH_STEP_LIMIT} steps on a text of {len(text)} characters; "
                    "narrow the text or the pattern"
                )
            instruction = program[pc]
            if instruction.kind == "match":
                return True
            if instruction.kind == "char":
                waiting.append(pc)
            elif instruction.kind == "jump":
                stack.append(instruction.first)
            elif instruction.kind == "split":
                stack.append(instruction.second)
                stack.append(instruction.first)
            elif _holds(instruction.assertion, text, index):
                stack.append(pc + 1)
        if index == len(text):
            break
        char = text[index]
        seeds = [pc + 1 for pc in waiting if (test := program[pc].test) is not None and test.test(char)]
    return False


def _evaluate(node: ast.expr, scope: Mapping[str, JsonValue]) -> object:
    match node:
        case ast.Constant(value=value):
            return value
        case ast.Name(id=name):
            return scope.get(name)
        case ast.Attribute(value=value, attr=attr):
            return _lookup(_evaluate(value, scope), attr)
        case ast.Subscript(value=value, slice=key):
            constant = _constant_key(key)
            if constant is None:
                raise _refuse(key)
            return _lookup(_evaluate(value, scope), constant)
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            current = _evaluate(left, scope)
            for op, comparator in zip(ops, comparators, strict=True):
                following = _evaluate(comparator, scope)
                if not _compare(op, current, following):
                    return False
                current = following
            return True
        case ast.BoolOp(op=op, values=operands):
            result: object = None
            for operand in operands:
                result = _evaluate(operand, scope)
                if isinstance(op, ast.And) and not result:
                    return result
                if isinstance(op, ast.Or) and result:
                    return result
            return result
        case ast.UnaryOp(op=op, operand=operand):
            value = _evaluate(operand, scope)
            return (not value) if isinstance(op, ast.Not) else -_number(value, "unary -")
        case ast.BinOp(left=left, op=op, right=right):
            return _binary(op, _evaluate(left, scope), _evaluate(right, scope))
        case ast.List(elts=elements) | ast.Tuple(elts=elements):
            return [_evaluate(element, scope) for element in elements]
        case ast.Call(func=ast.Name(id=name), args=arguments):
            return _call(name, [_evaluate(argument, scope) for argument in arguments])
        case _:
            raise _refuse(node)


@dataclass(frozen=True, slots=True)
class NodeProbe:
    node_id: str
    instance: str
    attempt: int
    op: str
    label: str | None
    src: str | None
    args: JsonValue
    session: str | None = None
    error: NodeError | None = None

    def scope(self, values: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            "node": self.node_id,
            "op": self.op,
            "label": self.label,
            "src": self.src,
            "attempt": self.attempt,
            "instance": self.instance,
            "args": self.args,
            "values": dict(values),
            "error": cast("JsonValue", self.error.model_dump(mode="json")) if self.error is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PauseDecision:
    reason: PauseReason
    breakpoint: str | None = None
    label: str | None = None


@dataclass
class ControlTicket:
    request: ControlRequest
    done: threading.Event = field(default_factory=threading.Event)
    response: ControlResponse | None = None


_OFF_MESSAGE = "debugging is off for this run; start it with --debug"
_TICKET_SLICE_SECONDS = 0.05


@dataclass(slots=True)
class _Entry:
    breakpoint: Breakpoint
    spec: BreakpointSpec
    condition: Condition | None


class Debugger:
    def __init__(self, *, enabled: bool, status: Callable[[], RunStatus], breakpoints: Sequence[str] = (),
                 break_on_error: bool = False,
                 journal: Callable[[str, Mapping[str, JsonValue]], None] | None = None) -> None:
        self.enabled = enabled
        self.status = status
        self.break_on_error = break_on_error
        self.journal = journal
        self._lock = threading.Condition()
        self._state: DebuggerState = "running" if enabled else "off"
        self._entries: dict[str, _Entry] = {}
        self._counter = 0
        self._pause: PauseInfo | None = None
        self._pause_requested = False
        self._abort = False
        self._closed: str | None = None
        self._tickets: deque[ControlTicket] = deque()
        self._taken: list[ControlTicket] = []
        self._pause_on_error: dict[str | None, bool] = {}
        for text in breakpoints:
            self.add(text)

    @property
    def state(self) -> DebuggerState:
        with self._lock:
            return self._state

    @property
    def pause_info(self) -> PauseInfo | None:
        with self._lock:
            return self._pause

    @property
    def abort_requested(self) -> bool:
        with self._lock:
            return self._abort

    def breakpoints(self) -> list[Breakpoint]:
        with self._lock:
            return [entry.breakpoint.model_copy() for entry in self._entries.values()]

    def add(self, text: str) -> Breakpoint:
        spec = parse_breakpoint(text)
        condition = Condition(spec.condition) if spec.condition is not None else None
        with self._lock:
            self._counter += 1
            created = Breakpoint(id=f"b{self._counter}", text=text, node=spec.node, op=spec.op, label=spec.label,
                                 condition=spec.condition)
            self._entries[created.id] = _Entry(created, spec, condition)
            copy = created.model_copy()
        self._emit("break.added", {"breakpoint": cast("JsonValue", copy.model_dump(mode="json"))})
        return copy

    def remove(self, breakpoint_id: str) -> bool:
        with self._lock:
            found = self._entries.pop(breakpoint_id, None) is not None
        if found:
            self._emit("break.removed", {"id": breakpoint_id})
        return found

    def set_pause_on_error(self, session: str | None, on: bool) -> None:
        with self._lock:
            self._pause_on_error[session] = on

    def before_node(self, probe: NodeProbe, values: Mapping[str, JsonValue]) -> PauseDecision | None:
        with self._lock:
            if self._state in ("off", "aborting") or self._closed is not None:
                return None
            entries = [entry for entry in self._entries.values()
                       if entry.breakpoint.enabled and (entry.spec.label is None or probe.label is not None)]
        matched = self._first_match(entries, probe, values, probe.label)
        with self._lock:
            if self._state in ("off", "aborting") or self._closed is not None:
                return None
            if matched is not None and matched.breakpoint.id in self._entries:
                matched.breakpoint.hits += 1
                self._pause_requested = False
                return PauseDecision("breakpoint", breakpoint=matched.breakpoint.id, label=probe.label)
            if self._state == "stepping":
                self._pause_requested = False
                return PauseDecision("step", label=probe.label)
            if self._pause_requested:
                self._pause_requested = False
                return PauseDecision("pause", label=probe.label)
        return None

    def at_source(self, probe: NodeProbe, label: str, hit: bool,
                  values: Mapping[str, JsonValue] | None = None) -> PauseDecision | None:
        with self._lock:
            if self._state in ("off", "aborting") or self._closed is not None:
                return None
            entries = [entry for entry in self._entries.values()
                       if entry.breakpoint.enabled and entry.spec.label is not None]
        matched = self._first_match(entries, probe, values if values is not None else {}, label)
        with self._lock:
            if self._state in ("off", "aborting") or self._closed is not None:
                return None
            if matched is not None and matched.breakpoint.id in self._entries:
                matched.breakpoint.hits += 1
                return PauseDecision("breakpoint", breakpoint=matched.breakpoint.id, label=label)
        return PauseDecision("source", label=label) if hit else None

    def at_error(self, probe: NodeProbe) -> PauseDecision | None:
        with self._lock:
            if self._state in ("off", "aborting") or self._closed is not None:
                return None
            if self.break_on_error or self._pause_on_error.get(probe.session, False):
                return PauseDecision("error", label=probe.label)
        return None

    def enter_pause(self, info: PauseInfo) -> None:
        with self._lock:
            if self._closed is not None or self._state == "off":
                return
            stale = self._drain(lambda _: True)
            self._pause = info
            self._pause_requested = False
            if self._state != "aborting":
                self._state = "paused"
            self._lock.notify_all()
        self._answer_stale(stale, "a new pause began before {action} was applied")

    def leave_pause(self, mode: ResumeMode) -> None:
        with self._lock:
            stale = self._drain(lambda _: True)
            self._pause = None
            if self._state in ("paused", "running", "stepping"):
                self._state = "stepping" if mode == "step" else "running"
            self._lock.notify_all()
        self._answer_stale(stale, "the run resumed before {action} was applied")

    def submit(self, request: ControlRequest, timeout: float = 30.0) -> ControlResponse:
        action = request.action
        with self._lock:
            closed = self._closed
            state = self._state
        if closed is not None:
            return self._response(False, closed)
        if action == "status":
            return self._response(True, f"debugger {state}")
        if action == "break_list":
            return self._response(True, f"{len(self.breakpoints())} breakpoint(s)")
        if action == "break_add":
            return self._break_add(request)
        if action == "break_remove":
            return self._break_remove(request)
        if action == "pause":
            return self._request_pause()
        if action == "abort":
            return self._abort_action(request, timeout)
        ticket = ControlTicket(request=request)
        with self._lock:
            state = self._state
            closed = self._closed
            queued = closed is None and state == "paused"
            if queued:
                self._tickets.append(ticket)
                self._lock.notify_all()
        if closed is not None:
            return self._response(False, closed)
        if state == "off":
            return self._response(False, _OFF_MESSAGE)
        if not queued:
            return self._response(False, f"the run is not paused (debugger {state}); {action} needs a paused run")
        return self._wait(ticket, timeout)

    def next_ticket(self, cancel: threading.Event, timeout: float | None = None) -> ControlTicket | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while not self._tickets:
                if cancel.is_set() or self._closed is not None or self._abort:
                    return None
                wait = _TICKET_SLICE_SECONDS
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    wait = min(wait, remaining)
                self._lock.wait(wait)
            ticket = self._tickets.popleft()
            self._taken.append(ticket)
            return ticket

    def answer(self, ticket: ControlTicket, response: ControlResponse) -> None:
        completed = self._response(response.ok, response.message, result=response.result)
        with self._lock:
            self._forget_taken(ticket)
            if ticket.done.is_set():
                return
            ticket.response = completed
            ticket.done.set()

    def request_abort(self) -> None:
        with self._lock:
            stale = self._abort_locked()
        self._answer_stale(stale, "the run is aborting; {action} was not applied")

    def close(self, message: str) -> None:
        with self._lock:
            self._closed = message
            self._state = "off"
            self._pause = None
            self._pause_requested = False
            pending = [*self._tickets, *self._taken]
            self._tickets.clear()
            self._taken.clear()
            self._lock.notify_all()
        response = self._response(False, message)
        for ticket in pending:
            self._complete(ticket, response)

    def _emit(self, event: str, detail: Mapping[str, JsonValue]) -> None:
        if self.journal is not None:
            self.journal(event, detail)

    def _first_match(self, entries: Sequence[_Entry], probe: NodeProbe, values: Mapping[str, JsonValue],
                     label: str | None) -> _Entry | None:
        scope: dict[str, JsonValue] | None = None
        for entry in entries:
            spec = entry.spec
            if spec.node is not None and spec.node != probe.node_id:
                continue
            if spec.op is not None and not fnmatchcase(probe.op, spec.op):
                continue
            if spec.label is not None and spec.label != label:
                continue
            if entry.condition is not None:
                if scope is None:
                    scope = probe.scope(values)
                    scope["label"] = label
                try:
                    holds = entry.condition.evaluate(scope)
                except ConditionError as error:
                    self._emit("break.error", {"id": entry.breakpoint.id, "message": str(error)})
                    continue
                if not holds:
                    continue
            return entry
        return None

    def _response(self, ok: bool, message: str, *, result: JsonValue = None) -> ControlResponse:
        with self._lock:
            state = self._state
            pause = self._pause
            breakpoints = [entry.breakpoint.model_copy() for entry in self._entries.values()]
        return ControlResponse(ok=ok, status=self.status(), debugger=state, pause=pause, breakpoints=breakpoints,
                               result=result, message=message)

    def _break_add(self, request: ControlRequest) -> ControlResponse:
        if not request.breakpoint:
            return self._response(False, "break_add needs a breakpoint spec")
        try:
            created = self.add(request.breakpoint)
        except ValueError as error:
            return self._response(False, str(error))
        return self._response(True, f"added breakpoint {created.id}")

    def _break_remove(self, request: ControlRequest) -> ControlResponse:
        if not request.id:
            return self._response(False, "break_remove needs a breakpoint id")
        if self.remove(request.id):
            return self._response(True, f"removed breakpoint {request.id}")
        return self._response(False, f"no breakpoint {request.id}")

    def _request_pause(self) -> ControlResponse:
        with self._lock:
            state = self._state
            if state in ("running", "stepping"):
                self._pause_requested = True
        if state == "off":
            return self._response(False, _OFF_MESSAGE)
        if state == "paused":
            return self._response(True, "the run is already paused")
        if state == "aborting":
            return self._response(False, "the run is aborting")
        return self._response(True, "the run pauses before the next node starts")

    def _abort_action(self, request: ControlRequest, timeout: float) -> ControlResponse:
        ticket = ControlTicket(request=request)
        with self._lock:
            paused = self._state == "paused"
            stale = self._abort_locked()
            if paused:
                self._tickets.append(ticket)
                self._lock.notify_all()
        self._answer_stale(stale, "the run is aborting; {action} was not applied")
        if not paused:
            return self._response(True, "abort requested")
        return self._wait(ticket, timeout)

    def _abort_locked(self) -> list[ControlTicket]:
        self._abort = True
        self._pause_requested = False
        if self._state != "off":
            self._state = "aborting"
        stale = self._drain(lambda ticket: ticket.request.action != "abort")
        self._lock.notify_all()
        return stale

    def _drain(self, chosen: Callable[[ControlTicket], bool]) -> list[ControlTicket]:
        stale = [ticket for ticket in self._tickets if chosen(ticket)]
        if stale:
            self._tickets = deque(ticket for ticket in self._tickets if not chosen(ticket))
        return stale

    def _answer_stale(self, stale: Sequence[ControlTicket], template: str) -> None:
        for ticket in stale:
            action = ticket.request.action
            if action == "abort":
                self._complete(ticket, self._response(True, "abort requested"))
            else:
                self._complete(ticket, self._response(False, template.format(action=action)))

    def _complete(self, ticket: ControlTicket, response: ControlResponse) -> None:
        with self._lock:
            if ticket.done.is_set():
                return
            ticket.response = response
            ticket.done.set()

    def _forget_taken(self, ticket: ControlTicket) -> None:
        for index, taken in enumerate(self._taken):
            if taken is ticket:
                del self._taken[index]
                return

    def _wait(self, ticket: ControlTicket, timeout: float) -> ControlResponse:
        if ticket.done.wait(timeout) and ticket.response is not None:
            return ticket.response
        with self._lock:
            withdrawn = any(queued is ticket for queued in self._tickets)
            if withdrawn:
                self._tickets = deque(queued for queued in self._tickets if queued is not ticket)
        if ticket.done.is_set() and ticket.response is not None:
            return ticket.response
        if withdrawn:
            return self._response(False, f"the run did not answer within {timeout:g} s")
        return self._response(
            False,
            f"the run took {ticket.request.action} but did not finish it within {timeout:g} s; "
            "it is still being applied, check the run status",
        )


__all__ = [
    "CONDITION_FUNCTIONS",
    "CONDITION_MAX_DEPTH",
    "CONDITION_MAX_LENGTH",
    "CONDITION_NAMES",
    "IMMEDIATE_ACTIONS",
    "PAUSED_ACTIONS",
    "BreakpointSpec",
    "Condition",
    "ConditionError",
    "ControlTicket",
    "Debugger",
    "NodeProbe",
    "PauseDecision",
    "ResumeMode",
    "parse_breakpoint",
]
