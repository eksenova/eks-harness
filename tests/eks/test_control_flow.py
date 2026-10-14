import pytest

import eks
from eks.ref import RefUsageError
from eks.tracing import PARALLEL_FLAG, TraceError, current
from eks_harness.ir.plan import Node

TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}
EMPTY = {"target": {"by": "test_id", "args": {"id": "empty-state"}}}


def tap(s: eks.Session, text: str) -> None:
    s.record_op("ui.tap", {"target": {"by": "text", "args": {"text": text}}})


def texts(nodes: list[Node]) -> list[str]:
    return [str(node.args["target"]["args"]["text"]) for node in nodes]  # type: ignore[index]


def test_when_and_otherwise_fill_then_and_else() -> None:
    @eks.flow(app="web-app", platform="web")
    def branching(s: eks.Session) -> None:
        with eks.when(s.record_op("ui.exists", EMPTY)):
            tap(s, "create")
        with eks.otherwise():
            tap(s, "first row")
            tap(s, "second row")
        tap(s, "after")

    exists, when, after = eks.plan(branching).nodes
    assert when.op == "ctl.when"
    assert when.args == {"cond": {"$ref": exists.id, "path": []}}
    assert when.branches is not None
    assert list(when.branches) == ["then", "else"]
    assert texts(when.branches["then"]) == ["create"]
    assert texts(when.branches["else"]) == ["first row", "second row"]
    first, second = when.branches["else"]
    assert first.after == []
    assert second.after == [first.id]
    assert after.after == [when.id]


def test_when_without_otherwise_has_only_then() -> None:
    @eks.flow(app="web-app", platform="web")
    def single(s: eks.Session) -> None:
        with eks.when(s.record_op("ui.exists", EMPTY)):
            pass

    _exists, when = eks.plan(single).nodes
    assert when.branches == {"then": []}


def test_when_takes_ref_paths() -> None:
    @eks.flow(app="web-app", platform="web")
    def paths(s: eks.Session) -> None:
        info = s.record_op("api.get", {"path": "/api/flags"})
        with eks.when(info.flags.beta):
            tap(s, "beta")

    info, when = eks.plan(paths).nodes
    assert when.args["cond"] == {"$ref": info.id, "path": ["flags", "beta"]}


def test_otherwise_without_a_preceding_when_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def orphan(s: eks.Session) -> None:
        tap(s, "x")
        with eks.otherwise():
            tap(s, "y")

    with pytest.raises(TraceError, match="must directly follow"):
        eks.plan(orphan)


def test_otherwise_at_the_start_of_a_scope_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def first(s: eks.Session) -> None:
        with eks.otherwise():
            tap(s, "y")

    with pytest.raises(TraceError, match="must directly follow"):
        eks.plan(first)


def test_otherwise_after_a_node_following_the_when_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def late(s: eks.Session) -> None:
        with eks.when(s.record_op("ui.exists", EMPTY)):
            tap(s, "a")
        tap(s, "between")
        with eks.otherwise():
            tap(s, "b")

    with pytest.raises(TraceError, match="must directly follow"):
        eks.plan(late)


def test_second_otherwise_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def twice(s: eks.Session) -> None:
        with eks.when(s.record_op("ui.exists", EMPTY)):
            tap(s, "a")
        with eks.otherwise():
            tap(s, "b")
        with eks.otherwise():
            tap(s, "c")

    with pytest.raises(TraceError, match="only one otherwise"):
        eks.plan(twice)


def test_otherwise_inside_the_when_body_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def inside(s: eks.Session) -> None:
        with eks.when(s.record_op("ui.exists", EMPTY)), eks.otherwise():
            tap(s, "b")

    with pytest.raises(TraceError, match="must directly follow"):
        eks.plan(inside)


def test_otherwise_after_a_literal_each_ending_with_a_when_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def spliced(s: eks.Session) -> None:
        with eks.each(["a", "b"]), eks.when(s.record_op("ui.exists", EMPTY)):
            tap(s, "then")
        with eks.otherwise():
            tap(s, "else")

    with pytest.raises(TraceError, match="must directly follow"):
        eks.plan(spliced)


def test_otherwise_inside_a_literal_each_body_applies_to_every_copy() -> None:
    @eks.flow(app="web-app", platform="web")
    def per_copy(s: eks.Session) -> None:
        with eks.each(["a", "b"]):
            with eks.when(s.record_op("ui.exists", EMPTY)):
                tap(s, "then")
            with eks.otherwise():
                tap(s, "else")

    whens = [node for node in eks.plan(per_copy).nodes if node.op == "ctl.when"]
    assert len(whens) == 2
    for when in whens:
        assert when.branches is not None
        assert list(when.branches) == ["then", "else"]


def test_nested_when() -> None:
    @eks.flow(app="web-app", platform="web")
    def nested(s: eks.Session) -> None:
        with eks.when(s.record_op("ui.exists", EMPTY)):
            inner = s.record_op("ui.exists", TAP)
            with eks.when(inner):
                tap(s, "inner then")
            with eks.otherwise():
                tap(s, "inner else")
        with eks.otherwise():
            tap(s, "outer else")

    _exists, outer = eks.plan(nested).nodes
    assert outer.branches is not None
    inner_exists, inner = outer.branches["then"]
    assert inner.op == "ctl.when"
    assert inner.after == [inner_exists.id]
    assert inner.branches is not None
    assert texts(inner.branches["then"]) == ["inner then"]
    assert texts(inner.branches["else"]) == ["inner else"]
    assert texts(outer.branches["else"]) == ["outer else"]


@pytest.mark.parametrize("value", [True, False, 1, "yes", None])
def test_when_with_a_python_value_raises_type_error(value: object) -> None:
    @eks.flow(app="web-app", platform="web")
    def python_value(s: eks.Session) -> None:
        with eks.when(value):  # type: ignore[arg-type]
            tap(s, "x")

    with pytest.raises(TypeError, match="plain if statement"):
        eks.plan(python_value)


def test_bool_of_a_ref_points_to_when() -> None:
    @eks.flow(app="web-app", platform="web")
    def truthy(s: eks.Session) -> None:
        if s.record_op("ui.exists", EMPTY):
            tap(s, "x")

    with pytest.raises(RefUsageError, match=r"eks\.when"):
        eks.plan(truthy)


def test_parallel_records_a_container_with_the_parallel_flag() -> None:
    flags: list[bool] = []

    @eks.flow(app="web-app", platform="web")
    def lanes(s: eks.Session) -> None:
        flags.append(current().has_flag(PARALLEL_FLAG))
        with eks.parallel():
            flags.append(current().has_flag(PARALLEL_FLAG))
            s.record_op("api.get", {"path": "/api/x"})
            with eks.when(s.record_op("ui.exists", EMPTY)):
                flags.append(current().has_flag(PARALLEL_FLAG))
        flags.append(current().has_flag(PARALLEL_FLAG))

    (block,) = eks.plan(lanes).nodes
    assert block.op == "ctl.parallel"
    assert block.args == {}
    assert flags == [False, True, True, False]


def test_parallel_nesting_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def nested(s: eks.Session) -> None:
        with eks.parallel(), eks.parallel():
            tap(s, "x")

    with pytest.raises(TraceError, match="cannot be nested"):
        eks.plan(nested)


def test_parallel_nesting_through_another_container_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def deep(s: eks.Session) -> None:
        with eks.parallel(), eks.when(s.record_op("ui.exists", EMPTY)), eks.parallel():
            tap(s, "x")

    with pytest.raises(TraceError, match="cannot be nested"):
        eks.plan(deep)


def test_sequential_parallel_blocks_are_allowed() -> None:
    @eks.flow(app="web-app", platform="web")
    def sequential(s: eks.Session) -> None:
        with eks.parallel():
            tap(s, "a")
        with eks.parallel():
            tap(s, "b")

    first, second = eks.plan(sequential).nodes
    assert second.after == [first.id]


def test_control_flow_outside_a_trace_raises() -> None:
    with pytest.raises(TraceError), eks.parallel():
        pass
    with pytest.raises(TraceError), eks.otherwise():
        pass
