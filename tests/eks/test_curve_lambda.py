import ast
import math
import random
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

import eks
from eks.effects import curves
from eks_harness.ir.catalog.curves import (
    LAMBDA_ARITY,
    LAMBDA_CONSTANT_VALUES,
    LAMBDA_CONSTANTS,
    LAMBDA_FUNCTIONS,
    LAMBDA_MAX_DEPTH,
    LAMBDA_MAX_INT,
    LAMBDA_MAX_LENGTH,
    Curve,
    Lambda,
    LambdaEvaluationError,
    check_lambda_expr,
    compile_lambda,
)


def _message(expr: str) -> str:
    with pytest.raises(ValidationError) as info:
        Lambda(expr=expr)
    errors = info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("expr",)
    return errors[0]["msg"]


def _tree(source: str) -> str:
    return ast.dump(ast.parse(source, mode="eval"))


def test_grammar_constants() -> None:
    assert LAMBDA_FUNCTIONS == frozenset(
        {
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan",
            "atan2",
            "sinh",
            "cosh",
            "tanh",
            "exp",
            "log",
            "log2",
            "log10",
            "sqrt",
            "pow",
            "abs",
            "floor",
            "ceil",
            "min",
            "max",
            "clamp",
            "hypot",
        }
    )
    assert LAMBDA_CONSTANTS == frozenset({"pi", "e", "tau"})
    assert dict(LAMBDA_CONSTANT_VALUES) == {"pi": math.pi, "e": math.e, "tau": math.tau}
    assert set(LAMBDA_ARITY) == LAMBDA_FUNCTIONS
    assert (LAMBDA_MAX_LENGTH, LAMBDA_MAX_DEPTH, LAMBDA_MAX_INT) == (1024, 64, 2**53)
    assert eks.curves is curves


@pytest.mark.parametrize(
    ("expr", "canonical"),
    [
        ("sin(t) * 0.5", "sin(t) * 0.5"),
        ("t < 1 and 2 or 3", "t < 1 and 2 or 3"),
        ("clamp(t, 0, 1)", "clamp(t, 0, 1)"),
        ("pi * t ** 2", "pi * t ** 2"),
        ("-t + +1 - 2 * 3 / 4 // 5 % 6", "-t + +1 - 2 * 3 / 4 // 5 % 6"),
        ("not t", "not t"),
        ("0 <= t < 1", "0 <= t < 1"),
        ("t == 1 or t != 2 and t > 0 and t >= 0 and t <= 9", "t == 1 or t != 2 and t > 0 and t >= 0 and t <= 9"),
        ("1 if t < 0.5 else 0", "1 if t < 0.5 else 0"),
        ("e ** -t * cos(tau * t)", "e ** -t * cos(tau * t)"),
        (
            "atan2(t, 1) + pow(t, 2) + hypot(t, 1, 2) + log(t + 1, 2)",
            "atan2(t, 1) + pow(t, 2) + hypot(t, 1, 2) + log(t + 1, 2)",
        ),
        ("min(t, 1) + max(t, 0, 2) + abs(floor(t) - ceil(t))", "min(t, 1) + max(t, 0, 2) + abs(floor(t) - ceil(t))"),
        (
            "sqrt(exp(t)) + log2(t + 1) + log10(t + 1) + sinh(t) + cosh(t) + tanh(t)",
            "sqrt(exp(t)) + log2(t + 1) + log10(t + 1) + sinh(t) + cosh(t) + tanh(t)",
        ),
        ("asin(0.5) + acos(0.5) + atan(t) + tan(t)", "asin(0.5) + acos(0.5) + atan(t) + tan(t)"),
        ("1e3 * t + 0x10", "1000.0 * t + 16"),
        ("(t\n + 1)", "t + 1"),
        ("sin( t )*0.5", "sin(t) * 0.5"),
        ("((t))", "t"),
        ("t+1_000", "t + 1000"),
        (".5*t", "0.5 * t"),
        ("(-t) ** 2", "(-t) ** 2"),
        ("(t ** 2) ** 3", "(t ** 2) ** 3"),
        ("t ** 2 ** 3", "t ** 2 ** 3"),
        ("t - (1 - t)", "t - (1 - t)"),
        ("(t - 1) - t", "t - 1 - t"),
        ("-(not t)", "-(not t)"),
        ("(t or 1) and 2", "(t or 1) and 2"),
        ("(t and 1) and 2", "(t and 1) and 2"),
        ("(t < 1) < 2", "(t < 1) < 2"),
        ("(1 if t else 2) if t else 3", "(1 if t else 2) if t else 3"),
        ("1 if (t if t else 2) else 3", "1 if (t if t else 2) else 3"),
        ("1 if t else 2 if t else 3", "1 if t else 2 if t else 3"),
        ("--t", "--t"),
        ("t ** -1e-05", "t ** -1e-05"),
        ("9007199254740992 * t", "9007199254740992 * t"),
    ],
)
def test_valid_expressions_store_the_canonical_form(expr: str, canonical: str) -> None:
    assert Lambda(expr=expr).expr == canonical
    assert check_lambda_expr(canonical) == canonical
    assert _tree(canonical) == _tree(expr)


def test_equivalent_spellings_store_and_hash_the_same_text() -> None:
    spellings = ["sin(t) * 0.5", "  sin(t)*0.5\n", "(sin( t ) * 0.5)", "sin(t)\t*\t.5"]
    dumps = {Lambda(expr=spelling).model_dump_json() for spelling in spellings}
    assert dumps == {'{"kind":"lambda","expr":"sin(t) * 0.5"}'}


def test_surrounding_whitespace_is_stripped() -> None:
    assert Lambda(expr="  sin(t)\n").expr == "sin(t)"
    assert Lambda(expr=" t ") == Lambda(expr="t")


def test_gap_reproduction_is_rejected() -> None:
    message = _message('__import__("os").system("x") + random()')
    assert "Call to '__import__(\"os\").system' is not allowed" in message
    assert "use t, e, pi, tau" in message


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        ("9**9**9**9", "overflow in '9 ** 9 ** 9'"),
        ("pow(10, pow(10, 10))", "overflow in 'pow(10, pow(10, 10))'"),
        ("10**10**10 if t > 0 else 0", "overflow in '10 ** 10 ** 10'"),
        ("sin(t) + 2 ** 2048", "overflow in '2 ** 2048'"),
        ("exp(1000) * t", "overflow in 'exp(1000)'"),
        ("t + 1e308 * 10", "overflow in '1e+308 * 10'"),
        ("t + 1 / 0", "division by zero in '1 / 0'"),
        ("0 and 1 // 0", "division by zero in '1 // 0'"),
        ("t % 0 if t else 0 % 0", "division by zero in '0 % 0'"),
        ("log(0) * t", "math domain error in 'log(0)'"),
        ("sqrt(-1) + t", "math domain error in 'sqrt(-1)'"),
        ("(-8) ** (1 / 3) * t", "math domain error in '(-8) ** (1 / 3)'"),
        ("log(2, 1) * t", "division by zero in 'log(2, 1)'"),
    ],
)
def test_constant_parts_must_evaluate_to_finite_floats(expr: str, fragment: str) -> None:
    message = _message(expr)
    assert message.startswith("Value error, expr: ")
    assert fragment in message
    assert "every part without t must evaluate to a finite float" in message


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        ('t # __import__("os").system("rm -rf /")', "comments ('#') are not allowed"),
        ('t # payload: __import__("os")', "comments ('#') are not allowed"),
        ("ｓｉｎ(ｔ)", "character 'ｓ' (U+FF53) is not allowed; expressions are printable ASCII"),
        ("\U0001d42d * 2", "character '\U0001d42d' (U+1D42D) is not allowed"),
        ("t +\\\n 1", "backslashes (line continuations) are not allowed"),
        ("t\x00", "character '\\x00' (U+0000) is not allowed"),
        ("t\r+ 1", "character '\\r' (U+000D) is not allowed"),
        ("t + 9007199254740993", "integer '9007199254740993' is beyond 2**53"),
        ("1e400 * t", "number '1e400' is not a finite float"),
        ("t" + " + t" * 400, f"the limit is {LAMBDA_MAX_LENGTH}"),
        ("-" * 70 + "t", f"nests deeper than {LAMBDA_MAX_DEPTH} levels"),
        ("t" + " + t" * 70, f"nests deeper than {LAMBDA_MAX_DEPTH} levels"),
        ("(" * 250 + "t" + ")" * 250, "invalid syntax"),
    ],
)
def test_lexical_and_size_limits(expr: str, fragment: str) -> None:
    message = _message(expr)
    assert message.startswith("Value error, expr: ")
    assert fragment in message


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        ('__import__("os").system("x")', "Call to '__import__(\"os\").system' is not allowed"),
        ('__import__("os")', "Call to '__import__' is not allowed"),
        ("random()", "Call to 'random' is not allowed"),
        ("t.real", "Attribute 't.real' is not allowed"),
        ('open("f")', "Call to 'open' is not allowed"),
        ("[t]", "List '[t]' is not allowed"),
        ("lambda: t", "Lambda 'lambda: t' is not allowed"),
        ("x + 1", "unknown names 'x'"),
        ("clamp(t, lo=0, hi=1)", "Call to 'clamp' with keyword arguments in 'clamp(t, lo=0, hi=1)' is not allowed"),
        ('"abc"', "Constant '\"abc\"' is not allowed"),
        ('sin("t")', "Constant '\"t\"' is not allowed"),
        ("True", "Constant 'True' is not allowed"),
        ("None", "Constant 'None' is not allowed"),
        ("1j", "Constant '1j' is not allowed"),
        ("...", "Constant '...' is not allowed"),
        ("t & 1", "operator '&' in 't & 1' is not allowed"),
        ("~t", "operator '~' in '~t' is not allowed"),
        ("t @ t", "operator '@' in 't @ t' is not allowed"),
        ("t is 1", "operator 'is' in 't is 1' is not allowed"),
        ("0 < t in (1,)", "operator 'in' in '0 < t in (1,)' is not allowed"),
        ("sin", "function 'sin' must be called"),
        ("pi()", "Call to 'pi' is not allowed"),
        ("t(1)", "Call to 't' is not allowed"),
        ("max(*t)", "Starred argument '*t' is not allowed"),
        ("sin(t, t)", "sin takes 1 argument, got 2"),
        ("min(t)", "min takes at least 2 arguments, got 1"),
        ("clamp(t, 0)", "clamp takes 3 arguments, got 2"),
        ("log(t, 2, 3)", "log takes 1 to 2 arguments, got 3"),
        ("(x := 1)", "NamedExpr 'x := 1' is not allowed"),
        ('f"{t}"', "JoinedStr 'f\"{t}\"' is not allowed"),
        ("t[0]", "Subscript 't[0]' is not allowed"),
        ("(t, 1)", "Tuple '(t, 1)' is not allowed"),
        ("{t: 1}", "Dict '{t: 1}' is not allowed"),
        ("[i for i in t]", "ListComp '[i for i in t]' is not allowed"),
        ("t +", "invalid syntax"),
        ("t = 1", "invalid syntax"),
        ("import os", "invalid syntax"),
    ],
)
def test_rejected_expressions(expr: str, fragment: str) -> None:
    message = _message(expr)
    assert message.startswith("Value error, expr: ")
    assert fragment in message


def test_unknown_names_are_all_listed_once() -> None:
    message = _message("x + y * x - sin(z)")
    assert "unknown names 'x', 'y', 'z'" in message


def test_blank_expressions_still_fail_the_field_constraints() -> None:
    with pytest.raises(ValidationError):
        Lambda(expr="")
    with pytest.raises(ValidationError):
        Lambda(expr="   ")


def test_eks_curves_expr_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match=r"Call to 'random' is not allowed"):
        eks.curves.expr("random()")
    with pytest.raises(ValidationError, match=r"unknown names 'x'"):
        curves.expr("x * 2")
    with pytest.raises(ValidationError, match=r"comments \('#'\) are not allowed"):
        eks.curves.expr('t # payload: __import__("os")')
    with pytest.raises(ValidationError, match=r"overflow in '9 \*\* 9 \*\* 9'"):
        eks.curves.expr("9**9**9**9")
    assert eks.curves.expr("sin(t) * 0.5") == Lambda(expr="sin(t) * 0.5")
    assert eks.curves.expr("sin(t)*.5").expr == "sin(t) * 0.5"


def test_plan_json_curve_is_checked() -> None:
    annotated: Any = Curve
    adapter: TypeAdapter[Any] = TypeAdapter(annotated)
    assert adapter.validate_python({"kind": "lambda", "expr": "sin(t)"}) == Lambda(expr="sin(t)")
    assert adapter.validate_python({"kind": "lambda", "expr": "sin(t)*2"}).expr == "sin(t) * 2"
    with pytest.raises(ValidationError, match=r"Call to '__import__' is not allowed"):
        adapter.validate_python({"kind": "lambda", "expr": "__import__('os')"})
    with pytest.raises(ValidationError, match=r"character 'ｓ' \(U\+FF53\) is not allowed"):
        Lambda.model_validate_json('{"kind": "lambda", "expr": "ｓｉｎ(ｔ) # x"}')
    with pytest.raises(ValidationError, match=r"overflow"):
        Lambda.model_validate_json('{"kind": "lambda", "expr": "10**10**10 if t > 0 else 0"}')


def test_check_lambda_expr_returns_the_canonical_source() -> None:
    assert check_lambda_expr(" t*2 ") == "t * 2"
    with pytest.raises(ValueError, match=r"Attribute 't.real' is not allowed"):
        check_lambda_expr("t.real")


@pytest.mark.parametrize(
    ("expr", "t", "expected"),
    [
        ("clamp(t, 0, 1)", 3.0, 1.0),
        ("clamp(t, 0, 1)", -2.0, 0.0),
        ("clamp(t, 0, 1)", 0.25, 0.25),
        ("tau * t", 1.0, math.tau),
        ("pi + e", 0.0, math.pi + math.e),
        ("1 if t > 0.5 else 0", 1.0, 1.0),
        ("1 if t > 0.5 else 0", 0.0, 0.0),
        ("log(t, 2)", 8.0, 3.0),
        ("log(t)", math.e, 1.0),
        ("sin(t) * 0.5", math.pi / 2, 0.5),
        ("7 // 2 + t", 0.0, 3.0),
        ("-7 // 2 + t", 0.0, -4.0),
        ("-1 % 3 + t", 0.0, 2.0),
        ("t % 1", 2.75, 0.75),
        ("t ** 2", 3.0, 9.0),
        ("pow(t, 0.5)", 9.0, 3.0),
        ("t < 1 and 2 or 3", 0.0, 2.0),
        ("t < 1 and 2 or 3", 5.0, 3.0),
        ("t and 4", 0.0, 0.0),
        ("t or 4", 0.0, 4.0),
        ("t or 4", 2.0, 2.0),
        ("not t", 0.0, 1.0),
        ("not t", 2.0, 0.0),
        ("0 <= t < 1", 0.5, 1.0),
        ("0 <= t < 1", 1.0, 0.0),
        ("t == 2", 2.0, 1.0),
        ("t != 2", 2.0, 0.0),
        ("floor(t) + ceil(t)", 1.5, 3.0),
        ("abs(t)", -2.5, 2.5),
        ("min(t, 1, 2) + max(t, 1, 2)", 0.0, 2.0),
        ("hypot(t, 4)", 3.0, 5.0),
        ("atan2(t, 1)", 1.0, math.pi / 4),
        ("sqrt(t) + exp(0) + log2(t) + log10(100)", 4.0, 7.0),
        ("asin(1) + acos(1) + atan(0) + tan(0) + sinh(0) + cosh(0) + tanh(0) + cos(t)", 0.0, math.pi / 2 + 2),
        ("--t", 2.0, 2.0),
        ("+t", -1.0, -1.0),
    ],
)
def test_compile_lambda_evaluates_every_construct(expr: str, t: float, expected: float) -> None:
    value = compile_lambda(expr)(t)
    assert type(value) is float
    assert value == pytest.approx(expected)


def test_every_function_is_evaluable() -> None:
    for name, (low, _high) in LAMBDA_ARITY.items():
        args = ", ".join(["0.5"] * low)
        assert math.isfinite(compile_lambda(f"{name}({args}) + t")(0.0)), name


def test_runtime_evaluation_short_circuits_like_the_conditional_reads() -> None:
    guarded = compile_lambda("log(t) if t > 0 else 0")
    assert guarded(0.0) == 0.0
    assert guarded(math.e) == pytest.approx(1.0)
    assert compile_lambda("t > 0 and 1 / t")(0.0) == 0.0
    assert compile_lambda("t or 1 / t")(2.0) == 2.0
    assert compile_lambda("t > 1 > 1 / t")(0.0) == 0.0


@pytest.mark.parametrize(
    ("expr", "t", "fragment"),
    [
        ("t ** 1e300", 2.0, "lambda 't ** 1e+300' at t=2.0: overflow in 't ** 1e+300'"),
        ("exp(t)", 1000.0, "overflow in 'exp(t)'"),
        ("1 / t", 0.0, "division by zero in '1 / t'"),
        ("log(t)", 0.0, "math domain error in 'log(t)'"),
        ("t * 1e308", 10.0, "overflow in 't * 1e+308'"),
        ("sin(t)", math.inf, "t=inf is not finite"),
        ("sin(t)", math.nan, "t=nan is not finite"),
    ],
)
def test_runtime_errors_are_lambda_evaluation_errors(expr: str, t: float, fragment: str) -> None:
    function = compile_lambda(expr)
    with pytest.raises(LambdaEvaluationError) as info:
        function(t)
    assert isinstance(info.value, ValueError)
    assert fragment in str(info.value)


def test_compile_lambda_rejects_what_the_model_rejects() -> None:
    with pytest.raises(ValueError, match=r"Call to 'random' is not allowed"):
        compile_lambda("random()")
    with pytest.raises(ValueError, match=r"overflow"):
        compile_lambda("9**9**9**9")


_LEAVES = ["t", "pi", "e", "tau", "1", "2", "0.5", "3.25"]
_BINARY = ["+", "-", "*", "/", "//", "%", "**"]
_COMPARE = ["<", "<=", ">", ">=", "==", "!="]


def _random_source(rng: random.Random, depth: int) -> str:
    if depth <= 0 or rng.random() < 0.2:
        return rng.choice(_LEAVES)
    choice = rng.randrange(8)
    left = _random_source(rng, depth - 1)
    right = _random_source(rng, depth - 1)
    match choice:
        case 0:
            return f"({left} {rng.choice(_BINARY)} {right})"
        case 1:
            return f"({rng.choice(['-', '+', 'not '])}{left})"
        case 2:
            return f"({left} {rng.choice(['and', 'or'])} {right} {rng.choice(['and', 'or'])} {left})"
        case 3:
            return f"({left} {rng.choice(_COMPARE)} {right} {rng.choice(_COMPARE)} {left})"
        case 4:
            return f"({left} if {right} else {left})"
        case 5:
            return f"min({left}, {right}, t)"
        case 6:
            return f"sin({left})"
        case _:
            return f"(t * {left} - {right})"


def test_canonical_form_round_trips_random_expressions() -> None:
    rng = random.Random(20261012)
    checked = 0
    for _ in range(3000):
        source = _random_source(rng, 5)
        try:
            canonical = check_lambda_expr(source)
        except ValueError:
            continue
        checked += 1
        assert _tree(canonical) == _tree(source), source
        assert check_lambda_expr(canonical) == canonical
        assert Lambda(expr=canonical) == Lambda(expr=source)
    assert checked > 1000
