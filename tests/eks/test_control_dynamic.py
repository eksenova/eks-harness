import importlib.util
import os
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest

import eks
from eks import by, expect, t
from eks.control import ALLOWED_DYNAMIC_GLOBALS
from eks.ref import Ref, type_of
from eks.tracing import TraceContext, TraceError, capture_function, tracing
from eks_harness.ir.hashing import digest
from eks_harness.ir.registry import Registry

ROW = {"target": {"by": "test_id", "args": {"id": "row"}}}
LIMIT = 3
NAMES = {"first": ["a", "b"]}
HELPER = object()


def fn_args(node: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", node.args["fn"])


def dynamic_node(plan: eks.Plan) -> Any:
    (node,) = [node for node, _ancestors in plan.walk() if node.op == "ctl.dynamic"]
    return node


def test_design_dynamic_snippet() -> None:
    results: list[Ref[Any]] = []

    @eks.flow(app="web-app", platform="web")
    def per_row_flow(s: eks.Session) -> Any:
        @eks.dynamic
        def per_row(n=s.record_op("ui.count", ROW)):  # noqa: B008
            return [s.ui.tap(by.test_id("row").nth(i)) for i in range(n)]

        results.append(per_row)
        return {"rows": per_row}

    plan = eks.plan(per_row_flow)
    count, node = plan.nodes
    assert count.op == "ui.count"
    assert node.op == "ctl.dynamic"
    assert node.out == "json"
    assert node.args["inputs"] == {"n": {"$ref": count.id, "path": []}}
    fn = fn_args(node)
    expected = textwrap.dedent("""\
        def per_row(n=s.record_op("ui.count", ROW)):  # noqa: B008
            return [s.ui.tap(by.test_id("row").nth(i)) for i in range(n)]
    """)
    assert fn["name"] == "per_row"
    assert fn["source"] == expected
    assert fn["hash"] == digest(expected.encode("utf-8"))
    assert fn["params"] == ["n"]
    assert fn["freevars"] == {"s": {"kind": "session", "value": "main"}}
    # range is an allowed sandbox builtin and by is a module global on the allow list; both are recorded
    assert fn["globals"] == ["by", "range"]
    assert plan.session is not None
    # a flow without seed, backend or persona holds only its lease
    assert len(node.uses) == 1
    assert {resource.kind for resource in plan.resources if resource.id in node.uses} == {"lease.web"}
    assert node.after == [count.id]
    (ref,) = results
    assert isinstance(ref, Ref)
    assert type_of(ref) == "json"
    assert plan.outputs == {"rows": {"$ref": node.id, "path": []}}


def test_dynamic_inputs_encode_literals_and_formatted_refs() -> None:
    @eks.flow(app="web-app", platform="web")
    def inputs(s: eks.Session) -> None:
        rows = s.record_op("api.get", {"path": "/api/rows"})

        @eks.dynamic
        def pick(limit=2, label=f"row-{rows.total}", when=t("1.5s"), first=rows.items.first):  # noqa: B008
            return [limit, label, when, first]

        assert type_of(pick) == "json"

    plan = eks.plan(inputs)
    rows, node = plan.nodes
    assert node.args["inputs"] == {
        "limit": 2,
        "label": {"$fmt": ["row-", {"$ref": rows.id, "path": ["total"]}]},
        "when": "1.5s",
        "first": {"$ref": rows.id, "path": ["items", 0]},
    }
    assert node.after == [rows.id]


def test_disallowed_global_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def helper_global(s: eks.Session) -> None:
        @eks.dynamic
        def uses_helper(n=1):
            return [HELPER, n]

    with pytest.raises(TraceError, match="HELPER"):
        eks.plan(helper_global)


def test_module_global_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def module_global(s: eks.Session) -> None:
        @eks.dynamic
        def uses_os(n=1):
            return os.getcwd() * n

    with pytest.raises(TraceError, match="'os'"):
        eks.plan(module_global)


def test_json_literal_globals_become_literal_freevars() -> None:
    @eks.flow(app="web-app", platform="web")
    def literal_globals(s: eks.Session) -> None:
        @eks.dynamic
        def uses_constants(n=1):
            return [LIMIT, NAMES, n, t, expect, eks.when]

    fn = fn_args(dynamic_node(eks.plan(literal_globals)))
    assert fn["freevars"] == {
        "LIMIT": {"kind": "literal", "value": 3},
        "NAMES": {"kind": "literal", "value": {"first": ["a", "b"]}},
    }
    assert fn["globals"] == ["eks", "expect", "t"]


def test_parameter_without_default_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def no_default(s: eks.Session) -> None:
        @eks.dynamic
        def needs_value(n):
            return n

    with pytest.raises(TraceError, match="'n' needs a default"):
        eks.plan(no_default)


def test_variadic_parameters_raise() -> None:
    @eks.flow(app="web-app", platform="web")
    def variadic(s: eks.Session) -> None:
        @eks.dynamic
        def rest(*values):
            return values

    with pytest.raises(TraceError, match=r"\*values"):
        eks.plan(variadic)


def test_closure_bindings_of_every_kind() -> None:
    @eks.flow(app="web-app", platform="web")
    def bindings(s: eks.Session) -> None:
        delay = t("300ms")
        target = by.test_id("row").nth(2)
        names = ["a", ("b", 1)]
        factory = by
        assertions = expect
        ui = s.ui

        @eks.dynamic
        def closure(n=1):
            return [s, delay, target, names, factory, assertions, ui, n]

    freevars = fn_args(dynamic_node(eks.plan(bindings)))["freevars"]
    assert freevars == {
        "assertions": {"kind": "factory", "value": "expect"},
        "delay": {"kind": "time", "value": "0.3s"},
        "factory": {"kind": "factory", "value": "by"},
        "names": {"kind": "literal", "value": ["a", ["b", 1]]},
        "s": {"kind": "session", "value": "main"},
        "target": {"kind": "target", "value": {"by": "test_id", "args": {"id": "row"}, "nth": 2}},
        "ui": {"kind": "namespace", "value": "main.ui"},
    }


def test_closure_variable_of_another_type_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def bad_closure(s: eks.Session) -> None:
        holder = object()

        @eks.dynamic
        def closure(n=1):
            return [holder, n]

    with pytest.raises(TraceError, match="'holder'"):
        eks.plan(bad_closure)


def test_closure_ref_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def ref_closure(s: eks.Session) -> None:
        count = s.record_op("ui.count", ROW)

        @eks.dynamic
        def closure(n=1):
            return [count, n]

    with pytest.raises(TraceError, match="parameter defaults"):
        eks.plan(ref_closure)


def dynamic_ids(path: Path, source: str) -> dict[str, str]:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("p4_dynamic_order_flow", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = eks.plan(module.ordered)
    return {
        cast("dict[str, Any]", node.args["fn"])["name"]: node.id
        for node, _ancestors in plan.walk() if node.op == "ctl.dynamic"
    }


def test_dynamic_ids_follow_the_function_not_its_position(tmp_path: Path) -> None:
    source = """\
        import eks


        @eks.flow(app="web-app", platform="web")
        def ordered(s: eks.Session):
            {first}

            {second}
    """
    first = """@eks.dynamic
            def first(n=1):
                return n"""
    second = """@eks.dynamic
            def second(n=2):
                return n"""
    path = tmp_path / "ordered.py"
    before = dynamic_ids(path, source.format(first=first, second=second))
    swapped = dynamic_ids(path, source.format(first=second, second=first))
    assert before["first"] != before["second"]
    assert swapped == before
    only_second = dynamic_ids(path, source.format(first="", second=second))
    assert only_second == {"second": before["second"]}


def test_dynamic_without_a_session_uses_no_resources(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="edit", name="no-session", registry=eks_registry)
    with tracing(ctx):
        @eks.dynamic
        def standalone(n=2):
            return list(range(n))

    (node,) = ctx.finalize().nodes
    assert node.uses == []
    assert fn_args(node)["globals"] == ["list", "range"]
    assert isinstance(standalone, Ref)


def test_dynamic_outside_a_trace_raises() -> None:
    with pytest.raises(TraceError, match=r"eks\.plan"):
        @eks.dynamic
        def outside(n=1):
            return n


def test_capture_function_strips_decorators_and_scans_nested_code() -> None:
    def outer(n=1):
        # no annotations here: they are evaluated when the def runs, so they are global loads too
        def inner():
            return by.text("x")

        values = tuple(item for item in (t,))
        return [inner(), values, n]

    source = capture_function(outer, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)
    assert source.source.startswith("def outer(n=1):\n")
    assert source.source.endswith("return [inner(), values, n]\n")
    assert source.globals == ("by", "t", "tuple")
    assert source.params == ("n",)

    def nested_helper(n=1):
        return [tuple(item for item in (HELPER,)), n]

    with pytest.raises(TraceError, match="HELPER"):
        capture_function(nested_helper, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)


def test_capture_function_rejects_lambdas() -> None:
    with pytest.raises(TraceError, match="def"):
        capture_function(lambda n=1: n)


def test_capture_function_normalizes_a_file_source(tmp_path: Path) -> None:
    path = tmp_path / "crlf_dynamic.py"
    path.write_bytes(
        b"def keep(*names):\r\n    def wrap(fn):\r\n        return fn\r\n    return wrap\r\n\r\n\r\n"
        b"@keep(\r\n    'a',\r\n    'b',\r\n)\r\n@keep()\r\n"
        b"def captured(n=1):\r\n    return n  \r\n\r\n\r\n",
    )
    spec = importlib.util.spec_from_file_location("p4_crlf_dynamic", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = capture_function(module.captured)
    assert captured.source == "def captured(n=1):\n    return n  \n"
    assert captured.hash == digest(captured.source.encode("utf-8"))
    assert captured.name == "captured"


def test_dynamic_with_a_column_zero_string_continuation() -> None:
    @eks.flow(app="web-app", platform="web")
    def queried(s: eks.Session) -> Any:
        count = s.record_op("ui.count", ROW)

        @eks.dynamic
        def rows(n=count):
            query = """
select *
  from rows
"""
            return [query, n]

        return rows

    fn = fn_args(dynamic_node(eks.plan(queried)))
    # the string continuation lines stay verbatim, the code lines lose the common prefix
    expected = 'def rows(n=count):\n    query = """\nselect *\n  from rows\n"""\n    return [query, n]\n'
    assert fn["source"] == expected
    assert fn["hash"] == digest(expected.encode("utf-8"))


def test_capture_function_keeps_line_numbers_with_unusual_line_breaks(tmp_path: Path) -> None:
    # \x0c and \x1c are line breaks for str.splitlines but not for the parser
    path = tmp_path / "formfeed_dynamic.py"
    path.write_bytes(
        b"def keep(*names):\n    def wrap(fn):\n        return fn\n    return wrap\n\n\n"
        b"def holder():\n"
        b"    @keep('a\x0cb', 'c\x1cd')\n"
        b"    def captured(n=1):\n"
        b"        text = '''\n"
        b"raw\n"
        b"'''\n"
        b"        return [text, n]\n"
        b"    return captured\n",
    )
    spec = importlib.util.spec_from_file_location("p4_formfeed_dynamic", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = capture_function(module.holder())
    assert captured.source == "def captured(n=1):\n    text = '''\nraw\n'''\n    return [text, n]\n"
    assert captured.params == ("n",)
