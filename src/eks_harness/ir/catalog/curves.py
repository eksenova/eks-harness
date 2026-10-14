from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.catalog.easing import Easing
from eks_harness.ir.catalog.keys import Keys
from eks_harness.ir.registry import Poly, curve
from eks_harness.ir.time import TICKS_PER_MS, Time, TimeSet


class _Envelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExpDecayEnv(_Envelope):
    kind: Literal["exp_decay_env"] = Field(default="exp_decay_env", description="Exponential decay envelope")
    tau: Duration = Field(description="Decay time constant")


class LinearEnv(_Envelope):
    kind: Literal["linear_env"] = Field(default="linear_env", description="Linear rise and fall envelope")
    rise: Duration = Field(description="Rise time")
    fall: Duration = Field(description="Fall time")


class BoxEnv(_Envelope):
    kind: Literal["box_env"] = Field(default="box_env", description="Constant envelope for a fixed width")
    width: Duration = Field(description="How long the envelope stays on")


class EasingEnv(_Envelope):
    kind: Literal["easing_env"] = Field(default="easing_env", description="Envelope shaped by an easing")
    duration: Duration = Field(description="Envelope length")
    easing: Easing = Field(description="Easing that shapes the envelope")
    direction: Literal["in", "out"] = Field(default="out", description="Ease out from the peak or in towards it")


Envelope = Annotated[ExpDecayEnv | LinearEnv | BoxEnv | EasingEnv, Field(discriminator="kind")]


class CurveBase(BaseModel, frozen=True, extra="forbid"):
    kind: str = Field(description="Curve kind")


@curve(description="An envelope fired on every time of a trigger set, scaled by an intensity ramp")
class BeatPulse(CurveBase, frozen=True):
    kind: Literal["beat_pulse"] = Field(default="beat_pulse", description="An envelope fired on every time of a trigger set, scaled by an intensity ramp")
    trigger: TimeSet = Field(description="Times that start a pulse, such as t.beats('downbeat')")
    envelope: Envelope = Field(description="Shape of each pulse")
    intensity: float | Keys[float] = Field(default=1.0, description="Pulse height, constant or keyframed")
    baseline: float = Field(default=0.0, description="Value between pulses")


@curve(description="Single exponential decay from the start")
class ExpDecay(CurveBase, frozen=True):
    kind: Literal["exp_decay"] = Field(default="exp_decay", description="Single exponential decay from the start")
    tau: Duration = Field(description="Decay time constant")
    baseline: float = Field(default=0.0, description="Value the curve decays to")


@curve(description="Sine wave")
class Sine(CurveBase, frozen=True):
    kind: Literal["sine"] = Field(default="sine", description="Sine wave")
    freq_hz: float = Field(gt=0, description="Frequency in Hz")
    phase: float = Field(default=0.0, description="Phase offset in radians")
    amp: float = Field(default=1.0, description="Amplitude")
    offset: float = Field(default=0.0, description="Value added to the wave")


@curve(description="Low-frequency oscillator with a triangle, saw or square shape")
class LFO(CurveBase, frozen=True):
    kind: Literal["lfo"] = Field(default="lfo", description="Low-frequency oscillator with a triangle, saw or square shape")
    shape: Literal["tri", "saw", "square"] = Field(description="Wave shape")
    freq_hz: float = Field(gt=0, description="Frequency in Hz")
    amp: float = Field(default=1.0, description="Amplitude")
    offset: float = Field(default=0.0, description="Value added to the wave")
    phase: float = Field(default=0.0, description="Phase offset from 0 to 1")


LAMBDA_ARITY: Mapping[str, tuple[int, int | None]] = MappingProxyType(
    {
        "sin": (1, 1),
        "cos": (1, 1),
        "tan": (1, 1),
        "asin": (1, 1),
        "acos": (1, 1),
        "atan": (1, 1),
        "atan2": (2, 2),
        "sinh": (1, 1),
        "cosh": (1, 1),
        "tanh": (1, 1),
        "exp": (1, 1),
        "log": (1, 2),
        "log2": (1, 1),
        "log10": (1, 1),
        "sqrt": (1, 1),
        "pow": (2, 2),
        "abs": (1, 1),
        "floor": (1, 1),
        "ceil": (1, 1),
        "min": (2, None),
        "max": (2, None),
        "clamp": (3, 3),
        "hypot": (1, None),
    }
)
LAMBDA_FUNCTIONS: frozenset[str] = frozenset(LAMBDA_ARITY)
LAMBDA_CONSTANT_VALUES: Mapping[str, float] = MappingProxyType({"pi": math.pi, "e": math.e, "tau": math.tau})
LAMBDA_CONSTANTS: frozenset[str] = frozenset(LAMBDA_CONSTANT_VALUES)
LAMBDA_VARIABLE = "t"
LAMBDA_MAX_LENGTH = 1024
LAMBDA_MAX_DEPTH = 64
LAMBDA_MAX_INT = 2**53

_COMPARE_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
_OPERATOR_SYMBOLS: Mapping[type[ast.AST], str] = MappingProxyType(
    {
        ast.UAdd: "+",
        ast.USub: "-",
        ast.Not: "not",
        ast.Invert: "~",
        ast.Add: "+",
        ast.Sub: "-",
        ast.Mult: "*",
        ast.Div: "/",
        ast.FloorDiv: "//",
        ast.Mod: "%",
        ast.Pow: "**",
        ast.MatMult: "@",
        ast.BitAnd: "&",
        ast.BitOr: "|",
        ast.BitXor: "^",
        ast.LShift: "<<",
        ast.RShift: ">>",
        ast.And: "and",
        ast.Or: "or",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
    }
)
LAMBDA_GRAMMAR_HINT = (
    f"use {LAMBDA_VARIABLE}, {', '.join(sorted(LAMBDA_CONSTANTS))}, int and float numbers, + - * / // % **, "
    "< <= > >= == !=, and, or, not, a if c else b and calls to "
    f"{', '.join(LAMBDA_ARITY)}"
)

_PREC_IF = 1
_PREC_OR = 2
_PREC_AND = 3
_PREC_NOT = 4
_PREC_COMPARE = 5
_PREC_ADD = 6
_PREC_MUL = 7
_PREC_UNARY = 8
_PREC_POW = 9
_PREC_ATOM = 10

_BINARY: Mapping[type[ast.operator], Callable[[float, float], float]] = MappingProxyType(
    {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: math.pow,
    }
)
_COMPARE: Mapping[type[ast.cmpop], Callable[[float, float], bool]] = MappingProxyType(
    {
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
    }
)


def _floor(x: float) -> float:
    return float(math.floor(x))


def _ceil(x: float) -> float:
    return float(math.ceil(x))


def _min(*values: float) -> float:
    return min(values)


def _max(*values: float) -> float:
    return max(values)


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


_FUNCTIONS: Mapping[str, Callable[..., float]] = MappingProxyType(
    {
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "asin": math.asin,
        "acos": math.acos,
        "atan": math.atan,
        "atan2": math.atan2,
        "sinh": math.sinh,
        "cosh": math.cosh,
        "tanh": math.tanh,
        "exp": math.exp,
        "log": math.log,
        "log2": math.log2,
        "log10": math.log10,
        "sqrt": math.sqrt,
        "pow": math.pow,
        "abs": math.fabs,
        "floor": _floor,
        "ceil": _ceil,
        "min": _min,
        "max": _max,
        "clamp": _clamp,
        "hypot": math.hypot,
    }
)


class LambdaEvaluationError(ValueError):
    pass


class _Undefined(Exception):
    def __init__(self, node: ast.expr, reason: str) -> None:
        super().__init__(reason)
        self.node = node
        self.reason = reason


def _segment(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    return segment if segment is not None else type(node).__name__


def _operator_problem(source: str, node: ast.AST, op: ast.AST) -> str:
    symbol = _OPERATOR_SYMBOLS.get(type(op), type(op).__name__)
    return f"operator {symbol!r} in {_segment(source, node)!r} is not allowed"


def _arity_text(low: int, high: int | None) -> str:
    if high is None:
        count = f"at least {low}"
    elif low == high:
        count = str(low)
    else:
        count = f"{low} to {high}"
    return f"{count} argument" if (low, high) == (1, 1) else f"{count} arguments"


def _lexical_problem(source: str) -> str | None:
    if len(source) > LAMBDA_MAX_LENGTH:
        return f"expression is {len(source)} characters long; the limit is {LAMBDA_MAX_LENGTH}"
    for char in source:
        if char == "#":
            return "comments ('#') are not allowed"
        if char == "\\":
            return "backslashes (line continuations) are not allowed"
        if char not in " \t\n" and not " " <= char <= "~":
            return f"character {char!r} (U+{ord(char):04X}) is not allowed; expressions are printable ASCII"
    return None


def _call_problem(source: str, node: ast.Call) -> str | None:
    func = node.func
    if not isinstance(func, ast.Name) or func.id not in LAMBDA_ARITY:
        return f"Call to {_segment(source, func)!r} is not allowed"
    if node.keywords:
        return f"Call to {func.id!r} with keyword arguments in {_segment(source, node)!r} is not allowed"
    starred = next((arg for arg in node.args if isinstance(arg, ast.Starred)), None)
    if starred is not None:
        return f"Starred argument {_segment(source, starred)!r} is not allowed"
    low, high = LAMBDA_ARITY[func.id]
    count = len(node.args)
    if count < low or (high is not None and count > high):
        return f"{func.id} takes {_arity_text(low, high)}, got {count} in {_segment(source, node)!r}"
    return None


def _number_problem(source: str, node: ast.Constant) -> str | None:
    value = node.value
    if type(value) is int and abs(value) > LAMBDA_MAX_INT:
        return f"integer {_segment(source, node)!r} is beyond 2**53, the largest exact float integer"
    if type(value) is float and not math.isfinite(value):
        return f"number {_segment(source, node)!r} is not a finite float"
    return None


def _children(source: str, node: ast.AST, unknown: list[str]) -> list[ast.expr]:
    match node:
        case ast.Constant(value=value) if type(value) in (int, float):
            problem = _number_problem(source, node)
            if problem is not None:
                raise ValueError(problem)
            return []
        case ast.Constant():
            raise ValueError(f"Constant {_segment(source, node)!r} is not allowed")
        case ast.Name(id=name, ctx=ast.Load()):
            if name == LAMBDA_VARIABLE or name in LAMBDA_CONSTANTS:
                return []
            if name in LAMBDA_ARITY:
                raise ValueError(f"function {name!r} must be called, as in {name}(t)")
            if name not in unknown:
                unknown.append(name)
            return []
        case ast.UnaryOp(op=ast.UAdd() | ast.USub() | ast.Not(), operand=operand):
            return [operand]
        case ast.BinOp(
            left=left,
            op=ast.Add() | ast.Sub() | ast.Mult() | ast.Div() | ast.FloorDiv() | ast.Mod() | ast.Pow(),
            right=right,
        ):
            return [left, right]
        case ast.BoolOp(op=ast.And() | ast.Or(), values=values):
            return list(values)
        case ast.Compare(left=left, ops=ops, comparators=comparators) if all(
            isinstance(op, _COMPARE_OPS) for op in ops
        ):
            return [left, *comparators]
        case ast.UnaryOp(op=op) | ast.BinOp(op=op) | ast.BoolOp(op=op):
            raise ValueError(_operator_problem(source, node, op))
        case ast.Compare(ops=ops):
            bad = next(op for op in ops if not isinstance(op, _COMPARE_OPS))
            raise ValueError(_operator_problem(source, node, bad))
        case ast.IfExp(test=test, body=body, orelse=orelse):
            return [test, body, orelse]
        case ast.Call(args=args):
            problem = _call_problem(source, node)
            if problem is not None:
                raise ValueError(problem)
            return list(args)
        case _:
            raise ValueError(f"{type(node).__name__} {_segment(source, node)!r} is not allowed")


def _operands(node: ast.expr) -> list[ast.expr]:
    match node:
        case ast.UnaryOp(operand=operand):
            return [operand]
        case ast.BinOp(left=left, right=right):
            return [left, right]
        case ast.BoolOp(values=values):
            return list(values)
        case ast.Compare(left=left, comparators=comparators):
            return [left, *comparators]
        case ast.IfExp(test=test, body=body, orelse=orelse):
            return [test, body, orelse]
        case ast.Call(args=args):
            return list(args)
        case _:
            return []


def _precedence(node: ast.expr) -> int:
    match node:
        case ast.IfExp():
            return _PREC_IF
        case ast.BoolOp(op=ast.Or()):
            return _PREC_OR
        case ast.BoolOp():
            return _PREC_AND
        case ast.UnaryOp(op=ast.Not()):
            return _PREC_NOT
        case ast.Compare():
            return _PREC_COMPARE
        case ast.BinOp(op=ast.Add() | ast.Sub()):
            return _PREC_ADD
        case ast.BinOp(op=ast.Pow()):
            return _PREC_POW
        case ast.BinOp():
            return _PREC_MUL
        case ast.UnaryOp():
            return _PREC_UNARY
        case _:
            return _PREC_ATOM


def _wrap(node: ast.expr, minimum: int) -> str:
    text = _render(node)
    return text if _precedence(node) >= minimum else f"({text})"


def _render(node: ast.expr) -> str:
    match node:
        case ast.Constant(value=float() as value):
            return repr(value)
        case ast.Constant(value=value):
            return str(value)
        case ast.Name(id=name):
            return name
        case ast.Call(func=ast.Name(id=name), args=args):
            return f"{name}({', '.join(_wrap(arg, _PREC_IF) for arg in args)})"
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            return f"not {_wrap(operand, _PREC_NOT)}"
        case ast.UnaryOp(op=op, operand=operand):
            return f"{_OPERATOR_SYMBOLS[type(op)]}{_wrap(operand, _PREC_UNARY)}"
        case ast.BinOp(left=left, op=ast.Pow(), right=right):
            return f"{_wrap(left, _PREC_ATOM)} ** {_wrap(right, _PREC_UNARY)}"
        case ast.BinOp(left=left, op=op, right=right):
            level = _precedence(node)
            return f"{_wrap(left, level)} {_OPERATOR_SYMBOLS[type(op)]} {_wrap(right, level + 1)}"
        case ast.BoolOp(op=op, values=values):
            level = _precedence(node)
            return f" {_OPERATOR_SYMBOLS[type(op)]} ".join(_wrap(value, level + 1) for value in values)
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            parts = [_wrap(left, _PREC_COMPARE + 1)]
            for op, comparator in zip(ops, comparators, strict=True):
                parts.append(_OPERATOR_SYMBOLS[type(op)])
                parts.append(_wrap(comparator, _PREC_COMPARE + 1))
            return " ".join(parts)
        case ast.IfExp(test=test, body=body, orelse=orelse):
            return f"{_wrap(body, _PREC_OR)} if {_wrap(test, _PREC_OR)} else {_wrap(orelse, _PREC_IF)}"
        case _:
            raise TypeError(f"{type(node).__name__} is outside the lambda grammar")


def _finite(node: ast.expr, value: float) -> float:
    if not math.isfinite(value):
        raise _Undefined(node, "overflow")
    return value


def _guarded(node: ast.expr, function: Callable[..., float], *args: float) -> float:
    try:
        return _finite(node, float(function(*args)))
    except OverflowError:
        raise _Undefined(node, "overflow") from None
    except ZeroDivisionError:
        raise _Undefined(node, "division by zero") from None
    except ValueError:
        raise _Undefined(node, "math domain error") from None


def _evaluate(node: ast.expr, t: float) -> float:
    match node:
        case ast.Constant(value=int() | float() as value):
            return float(value)
        case ast.Name(id=name):
            return t if name == LAMBDA_VARIABLE else LAMBDA_CONSTANT_VALUES[name]
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            return 1.0 if _evaluate(operand, t) == 0 else 0.0
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_evaluate(operand, t)
        case ast.UnaryOp(operand=operand):
            return _evaluate(operand, t)
        case ast.BinOp(left=left, op=op, right=right):
            return _guarded(node, _BINARY[type(op)], _evaluate(left, t), _evaluate(right, t))
        case ast.BoolOp(op=ast.And(), values=values):
            result = 0.0
            for value in values:
                result = _evaluate(value, t)
                if result == 0:
                    return result
            return result
        case ast.BoolOp(values=values):
            result = 0.0
            for value in values:
                result = _evaluate(value, t)
                if result != 0:
                    return result
            return result
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            current = _evaluate(left, t)
            for op, comparator in zip(ops, comparators, strict=True):
                following = _evaluate(comparator, t)
                if not _COMPARE[type(op)](current, following):
                    return 0.0
                current = following
            return 1.0
        case ast.IfExp(test=test, body=body, orelse=orelse):
            return _evaluate(body, t) if _evaluate(test, t) != 0 else _evaluate(orelse, t)
        case ast.Call(func=ast.Name(id=name), args=args):
            return _guarded(node, _FUNCTIONS[name], *(_evaluate(arg, t) for arg in args))
        case _:
            raise TypeError(f"{type(node).__name__} is outside the lambda grammar")


def _fold(node: ast.expr) -> bool:
    folded = [_fold(operand) for operand in _operands(node)]
    constant = all(folded)
    if isinstance(node, ast.Name) and node.id == LAMBDA_VARIABLE:
        return False
    if constant:
        _evaluate(node, 0.0)
    return constant


def _parse(expr: str) -> ast.expr:
    source = expr.strip()
    problem = _lexical_problem(source)
    if problem is not None:
        raise ValueError(f"expr: {problem}; {LAMBDA_GRAMMAR_HINT}")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"expr: invalid syntax ({exc.msg}) in {source!r}; {LAMBDA_GRAMMAR_HINT}") from None
    except (RecursionError, MemoryError):
        raise ValueError(f"expr: expression is nested too deeply; {LAMBDA_GRAMMAR_HINT}") from None
    unknown: list[str] = []
    pending: list[tuple[ast.expr, int]] = [(tree.body, 1)]
    while pending:
        node, depth = pending.pop()
        if depth > LAMBDA_MAX_DEPTH:
            raise ValueError(f"expr: expression nests deeper than {LAMBDA_MAX_DEPTH} levels; {LAMBDA_GRAMMAR_HINT}")
        try:
            children = _children(source, node, unknown)
        except ValueError as exc:
            raise ValueError(f"expr: {exc}; {LAMBDA_GRAMMAR_HINT}") from None
        pending.extend((child, depth + 1) for child in reversed(children))
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        raise ValueError(f"expr: unknown names {names}; {LAMBDA_GRAMMAR_HINT}")
    try:
        _fold(tree.body)
    except _Undefined as exc:
        raise ValueError(
            f"expr: {exc.reason} in {_render(exc.node)!r}; every part without t must evaluate to a finite float"
        ) from None
    return tree.body


def check_lambda_expr(expr: str) -> str:
    return _render(_parse(expr))


def compile_lambda(expr: str) -> Callable[[float], float]:
    body = _parse(expr)
    canonical = _render(body)

    def evaluate(t: float) -> float:
        if not math.isfinite(t):
            raise LambdaEvaluationError(f"lambda {canonical!r}: t={t!r} is not finite")
        try:
            return _evaluate(body, float(t))
        except _Undefined as exc:
            raise LambdaEvaluationError(f"lambda {canonical!r} at t={t!r}: {exc.reason} in {_render(exc.node)!r}") from None

    return evaluate


@curve(description="Sandboxed expression of t in seconds")
class Lambda(CurveBase, frozen=True):
    kind: Literal["lambda"] = Field(default="lambda", description="Sandboxed expression of t in seconds")
    expr: str = Field(
        min_length=1,
        pattern=r"\S",
        description=(
            "Arithmetic expression of t (seconds) evaluated per frame in IEEE double arithmetic, stored in "
            "canonical form; printable ASCII, at most 1024 characters and 64 levels deep; grammar: t, pi, e, tau, "
            "int (|n| <= 2**53) and float numbers, + - * / // % **, < <= > >= == !=, and, or, not, a if c else b "
            "and positional calls to LAMBDA_FUNCTIONS; every part without t must evaluate to a finite number "
            "(see docs/dsl/contracts-m0.md 10.4)"
        ),
    )

    @field_validator("expr")
    @classmethod
    def _check_expr(cls, value: str) -> str:
        return check_lambda_expr(value)


@curve(description="Damped spring settling on a target value")
class Spring(CurveBase, frozen=True):
    kind: Literal["spring"] = Field(default="spring", description="Damped spring settling on a target value")
    target: float = Field(description="Value the spring settles at")
    stiffness: float = Field(default=100.0, gt=0, description="Spring stiffness")
    damping: float = Field(default=10.0, gt=0, description="Damping")
    initial: float = Field(default=0.0, description="Starting value")


@curve(description="Seeded uniform noise, optionally held; the seed derives from the plan when unset")
class Noise(CurveBase, frozen=True):
    kind: Literal["noise"] = Field(default="noise", description="Seeded uniform noise, optionally held; the seed derives from the plan when unset")
    seed: int | None = Field(default=None, description="Random seed; derived from the node hash and root seed when unset")
    min_value: float = Field(default=0.0, description="Smallest value")
    max_value: float = Field(default=1.0, description="Largest value")
    hold: Duration | None = Field(default=None, description="How long each random value is held; smooth noise when unset")


@curve(description="Seeded random pick from values, held for a while")
class RandomChoice(CurveBase, frozen=True):
    kind: Literal["random_choice"] = Field(default="random_choice", description="Seeded random pick from values, held for a while")
    seed: int | None = Field(default=None, description="Random seed; derived from the node hash and root seed when unset")
    values: tuple[float, ...] = Field(min_length=1, description="Values to choose from")
    hold: Duration = Field(default=Time(ticks=500 * TICKS_PER_MS), description="How long each choice is held")


@curve(description="Round robin through values, advancing every hold")
class Step(CurveBase, frozen=True):
    kind: Literal["step"] = Field(default="step", description="Round robin through values, advancing every hold")
    values: tuple[float, ...] = Field(min_length=1, description="Values stepped through in order")
    hold: Duration = Field(description="How long each value is held")


@curve(description="Cubic Bezier value curve from p0 to p3 over a duration")
class Bezier(CurveBase, frozen=True):
    kind: Literal["bezier"] = Field(default="bezier", description="Cubic Bezier value curve from p0 to p3 over a duration")
    p0: float = Field(description="Start value")
    p1: float = Field(description="First control value")
    p2: float = Field(description="Second control value")
    p3: float = Field(description="End value")
    duration: Duration = Field(description="Length of the curve")


@curve(description="Damped bounce: amplitude * exp(-decay * t) * |sin(2 pi t / period)|")
class Bounce(CurveBase, frozen=True):
    kind: Literal["bounce"] = Field(default="bounce", description="Damped bounce: amplitude * exp(-decay * t) * |sin(2 pi t / period)|")
    amplitude: float = Field(description="Height of the first bounce")
    period: Duration = Field(description="Length of one bounce")
    decay: float = Field(default=1.0, description="How fast the bounces shrink")


Curve = Annotated[SerializeAsAny[CurveBase], Poly("curve")]

__all__ = [
    "LAMBDA_ARITY",
    "LAMBDA_CONSTANTS",
    "LAMBDA_CONSTANT_VALUES",
    "LAMBDA_FUNCTIONS",
    "LAMBDA_GRAMMAR_HINT",
    "LAMBDA_MAX_DEPTH",
    "LAMBDA_MAX_INT",
    "LAMBDA_MAX_LENGTH",
    "LAMBDA_VARIABLE",
    "LFO",
    "BeatPulse",
    "Bezier",
    "Bounce",
    "BoxEnv",
    "Curve",
    "CurveBase",
    "EasingEnv",
    "Envelope",
    "ExpDecay",
    "ExpDecayEnv",
    "Lambda",
    "LambdaEvaluationError",
    "LinearEnv",
    "Noise",
    "RandomChoice",
    "Sine",
    "Spring",
    "Step",
    "check_lambda_expr",
    "compile_lambda",
]
