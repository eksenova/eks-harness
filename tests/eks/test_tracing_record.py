from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import t
from eks.ref import Ref, node_of, path_of, type_of
from eks.tracing import TraceContext, TraceError, current, record, tracing
from eks_harness.ir.ops.api import Get
from eks_harness.ir.plan import Node, Plan, Requirement
from eks_harness.ir.refs import RJson
from eks_harness.ir.registry import Registry, contributing

TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


def plan_of(fn: Any, **params: Any) -> Plan:
    return eks.plan(fn, **params)


def ops(plan: Plan) -> list[str]:
    return [node.op for node, _ancestors in plan.walk()]


def test_args_are_normalized_with_exclude_none() -> None:
    @eks.flow(app="web-app", platform="web")
    def normalized(s: eks.Session) -> None:
        s.record_op("api.get", {"path": "/api/x", "query": None, "headers": None})
        s.record_op("api.get", Get(path="/api/y"))

    plan = plan_of(normalized)
    first, second = plan.nodes
    assert first.args == {"path": "/api/x"}
    assert second.args == {"path": "/api/y"}


def test_defaults_of_the_model_are_stored() -> None:
    @eks.flow(app="web-app", platform="web")
    def defaults(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)

    (node,) = plan_of(defaults).nodes
    # non-None defaults are explicit in the plan so hashes follow default changes
    assert node.args["settle"] is True
    assert node.args["target"] == {"by": "text", "args": {"text": "Kaydet"}}


def test_f_string_with_a_ref_becomes_fmt() -> None:
    @eks.flow(app="web-app", platform="web")
    def formatted(s: eks.Session) -> None:
        rows = s.record_op("api.get", {"path": "/api/rows"})
        s.record_op("ui.nav", {"route": f"/tenant/invoice/list?branchId={rows.items.first.id}"})

    plan = plan_of(formatted)
    rows, nav = plan.nodes
    assert nav.args["route"] == {
        "$fmt": ["/tenant/invoice/list?branchId=", {"$ref": rows.id, "path": ["items", 0, "id"]}],
    }


def test_plain_ref_argument_is_encoded() -> None:
    @eks.flow(app="web-app", platform="web")
    def plain(s: eks.Session) -> None:
        text = s.record_op("ui.text", {"target": {"by": "test_id", "args": {"id": "amount"}}})
        s.record_op("ui.fill", {"target": {"by": "test_id", "args": {"id": "copy"}}, "value": text})

    text, fill = plan_of(plain).nodes
    assert fill.args["value"] == {"$ref": text.id, "path": []}


def test_ref_to_unknown_node_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def unknown(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": {"$ref": "n0000000000", "path": []}})

    with pytest.raises(TraceError, match="n0000000000"):
        plan_of(unknown)


def test_ref_of_another_trace_raises() -> None:
    leaked: list[Ref[Any]] = []

    @eks.flow(app="web-app", platform="web")
    def producer(s: eks.Session) -> None:
        leaked.append(s.record_op("api.get", {"path": "/api/x"}))

    @eks.flow(app="web-app", platform="web")
    def consumer(s: eks.Session) -> None:
        s.record_op("ui.fill", {"target": {"by": "test_id", "args": {"id": "x"}}, "value": leaked[0]})

    plan_of(producer)
    with pytest.raises(TraceError, match="not recorded in this plan trace"):
        plan_of(consumer)


def test_item_outside_each_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def stray(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": {"$item": "n0000000000", "path": []}})

    with pytest.raises(TraceError, match=r"eks\.each"):
        plan_of(stray)


def test_container_op_with_record_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def container(s: eks.Session) -> None:
        cond = s.record_op("ui.exists", TAP)
        s.ctx.record("ctl.when", {"cond": cond})

    with pytest.raises(TraceError, match="container op"):
        plan_of(container)


def test_plain_op_with_container_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="direct", registry=eks_registry)
    with tracing(ctx), pytest.raises(TraceError, match="not a container op"), ctx.container("media.source", {"uri": "file:a"}):
        pass


def test_invalid_args_raise_validation_error() -> None:
    @eks.flow(app="web-app", platform="web")
    def invalid(s: eks.Session) -> None:
        s.record_op("api.get", {"path": "/x", "bogus": 1})

    with pytest.raises(ValidationError):
        plan_of(invalid)


def test_plugin_op_lands_in_requires() -> None:
    with contributing("acme.tools", "1.2.0"):
        @eks.op("acme", name="ping", out="json")
        class Ping(eks.Args):
            host: str

    @eks.op("local", name="noop")
    class Noop(eks.Args):
        pass

    @eks.flow(app="web-app", platform="web")
    def plugin(s: eks.Session) -> None:
        s.ctx.record("local.noop")
        s.ctx.record("acme.ping", Ping(host="example.test"))
        s.ctx.record("acme.ping", {"host": "other.test"})

    plan = plan_of(plugin)
    assert ops(plan) == ["local.noop", "acme.ping", "acme.ping"]
    # project ops and built-ins never become requirements
    assert plan.requires == [Requirement(plugin="acme.tools", version="1.2.0")]
    assert Noop.__name__ == "Noop"


def test_explicit_require_is_sorted_by_plugin(eks_registry: Registry) -> None:
    from eks_harness.ir.registry import Provenance

    ctx = TraceContext(kind="flow", name="requires", registry=eks_registry)
    ctx.require(Provenance("zeta", "2"))
    ctx.require(Provenance("alpha", "1"))
    ctx.require(Provenance("eks", "0"))
    ctx.require(Provenance("project", ""))
    plan = ctx.finalize()
    assert [(item.plugin, item.version) for item in plan.requires] == [("alpha", "1"), ("zeta", "2")]


def test_out_label_and_pin_are_stored() -> None:
    @eks.op("demo", name="values", out="json")
    class Values(eks.Args):
        value: RJson = None

    @eks.flow(app="web-app", platform="web")
    def stored(s: eks.Session) -> None:
        s.ctx.record("demo.values", {"value": [1, 2]}, out="list[int]", label="the values", pin=t("bar:2"))
        s.record_op("ui.tap", TAP, at=t("beat:4"), label="tap it")

    values, tap = plan_of(stored).nodes
    assert (values.out, values.label, values.pin) == ("list[int]", "the values", t("bar:2"))
    assert (tap.out, tap.label, tap.pin) == ("none", "tap it", t("beat:4"))
    assert Values.model_fields["value"].default is None


def test_label_and_id_key_built_from_a_ref_raise() -> None:
    # a ref inside a label would leak sentinel text into the plan and break byte-identical JSON
    @eks.flow(app="web-app", platform="web")
    def labelled(s: eks.Session) -> None:
        r = s.record_op("api.get", {"path": "/api/x"})
        s.record_op("ui.tap", TAP, label=f"tap {r.name}")

    with pytest.raises(TraceError, match="label must be plain text"):
        plan_of(labelled)

    @eks.flow(app="web-app", platform="web")
    def keyed(s: eks.Session) -> None:
        r = s.record_op("api.get", {"path": "/api/x"})
        s.record_op("ui.tap", TAP, id_key=f"tap-{r.name}")

    with pytest.raises(TraceError, match="id_key must be plain text"):
        plan_of(keyed)


def test_pin_must_be_a_time(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="pins", registry=eks_registry)
    with tracing(ctx), pytest.raises(TypeError, match="pin"):
        ctx.record("media.source", {"uri": "file:a.mp4"}, pin="1s")  # type: ignore[arg-type]


def test_at_on_a_non_pinnable_op_raises() -> None:
    @eks.flow(app="web-app", platform="web")
    def pinned(s: eks.Session) -> None:
        s.record_op("api.get", {"path": "/x"}, at=t("1s"))

    with pytest.raises(TraceError, match="cannot be pinned"):
        plan_of(pinned)


def test_invalid_out_type_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="outs", registry=eks_registry)
    with tracing(ctx), pytest.raises(TraceError, match="ref type"):
        ctx.record("media.source", {"uri": "file:a.mp4"}, out="tuple[int]")


def test_unknown_resource_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="uses", registry=eks_registry)
    with tracing(ctx), pytest.raises(TraceError, match="not declared"):
        ctx.record("media.source", {"uri": "file:a.mp4"}, uses=["rzzzzzzz"])


def test_returned_ref_types() -> None:
    refs: dict[str, Ref[Any]] = {}

    @eks.flow(app="web-app", platform="web")
    def typed(s: eks.Session) -> None:
        refs["get"] = s.record_op("api.get", {"path": "/x"})
        refs["count"] = s.record_op("ui.count", TAP)
        refs["exists"] = s.record_op("ui.exists", TAP)
        refs["tap"] = s.record_op("ui.tap", TAP)
        refs["media"] = s.ctx.record("media.source", {"uri": "file:clip.mp4"})
        refs["override"] = s.ctx.record("api.get", {"path": "/y"}, uses=[s.resources["backend"]], out="list[json]")

    plan = plan_of(typed)
    assert {name: type_of(ref) for name, ref in refs.items()} == {
        "get": "json", "count": "int", "exists": "bool", "tap": "none", "media": "media", "override": "list[json]",
    }
    ids = [node.id for node in plan.nodes]
    assert [node_of(ref) for ref in refs.values()] == ids
    assert all(path_of(ref) == () for ref in refs.values())
    assert isinstance(refs["get"], Ref)


def test_module_level_record_uses_the_current_trace(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="module", registry=eks_registry)
    with tracing(ctx):
        assert current() is ctx
        ref = record("media.source", {"uri": "file:a.mp4"})
    plan = ctx.finalize()
    assert [node.id for node in plan.nodes] == [node_of(ref)]
    assert isinstance(plan.nodes[0], Node)


def test_record_outside_a_trace_raises() -> None:
    with pytest.raises(TraceError, match=r"eks\.plan"):
        record("media.source", {"uri": "file:a.mp4"})
    with pytest.raises(TraceError, match=r"eks\.plan"):
        current()


def test_recording_after_finalize_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="late", registry=eks_registry)
    ctx.finalize()
    with tracing(ctx), pytest.raises(TraceError, match="finalized"):
        ctx.record("media.source", {"uri": "file:a.mp4"})
    with pytest.raises(TraceError, match="finalized"):
        ctx.finalize()


def test_src_is_the_innermost_user_frame() -> None:
    @eks.flow(app="web-app", platform="web")
    def located(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)

    (node,) = plan_of(located).nodes
    assert node.src is not None
    file, line = node.src.rsplit(":", 1)
    assert file == "tests/eks/test_tracing_record.py"
    assert int(line) == located.__code__.co_firstlineno + 2


def test_node_lookup_and_last_node(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="lookup", registry=eks_registry)
    assert ctx.last_node() is None
    with tracing(ctx):
        ref = ctx.record("media.source", {"uri": "file:a.mp4"})
    node = ctx.node(node_of(ref))
    assert ctx.last_node() is node
    with pytest.raises(TraceError, match="not recorded"):
        ctx.node("n0000000000")
