from typing import Any

import pytest

import eks
from eks.ref import Ref, node_of
from eks.tracing import TraceContext, TraceError, tracing
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.refs import RJson, RMedia
from eks_harness.ir.registry import Registry

TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


def register_demo_ops() -> None:
    @eks.op("demo", name="use", out="json")
    class Use(eks.Args):
        value: RJson = None

    @eks.op("demo", name="render", out="media", pure=True)
    class Render(eks.Args):
        clip: RMedia

    assert Use.__name__ == "Use"
    assert Render.__name__ == "Render"


def index(plan: Plan) -> dict[str, Node]:
    return {node.id: node for node, _ancestors in plan.walk()}


def test_program_ops_chain_in_program_order() -> None:
    @eks.flow(app="web-app", platform="web")
    def chained(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/a"})
        s.record_op("ui.tap", TAP)
        s.record_op("api.get", {"path": "/api/x"})

    first, second, third = eks.plan(chained).nodes
    assert first.after == []
    assert second.after == [first.id]
    assert third.after == [second.id]


def test_pure_op_has_no_tail_edge_and_does_not_become_the_tail() -> None:
    register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def pure(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/a"})
        clip = s.ctx.record("media.source", {"uri": "file:clip.mp4"})
        s.ctx.record("demo.render", {"clip": clip})
        s.record_op("ui.tap", TAP)

    nav, source, render, tap = eks.plan(pure).nodes
    assert source.after == []
    assert render.after == [source.id]
    # the tap chains after the nav: pure nodes never join the program chain
    assert tap.after == [nav.id]


def test_ref_dependencies_follow_the_tail_in_document_order() -> None:
    register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def refs(s: eks.Session) -> None:
        a = s.record_op("api.get", {"path": "/a"})
        b = s.record_op("api.get", {"path": "/b"})
        s.record_op("ui.nav", {"route": "/c"})
        s.ctx.record("demo.use", {"value": [b.items, {"x": a.id}, f"{b.name}", a]})

    a, b, c, use = eks.plan(refs).nodes
    assert use.after == [c.id, b.id, a.id]


def test_reference_to_the_tail_is_not_duplicated() -> None:
    register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def tail(s: eks.Session) -> None:
        a = s.record_op("api.get", {"path": "/a"})
        s.ctx.record("demo.use", {"value": [a, a.id]})

    a, use = eks.plan(tail).nodes
    assert use.after == [a.id]


def test_parallel_lanes_and_the_join() -> None:
    @eks.flow(app="web-app", platform="web")
    def lanes(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/start"})
        with eks.parallel():
            s.record_op("api.get", {"path": "/api/a"})
            s.record_op("ui.nav", {"route": "/x"})
            s.record_op("api.get", {"path": "/api/b"})
            s.record_op("ui.tap", TAP)
        s.record_op("ui.nav", {"route": "/end"})

    plan = eks.plan(lanes)
    start, block, end = plan.nodes
    assert block.op == "ctl.parallel"
    assert block.after == [start.id]
    assert block.branches is not None
    api_a, nav_x, api_b, tap = block.branches["body"]
    assert api_a.uses != nav_x.uses
    assert api_a.after == []
    assert nav_x.after == []
    assert api_b.after == [api_a.id]
    assert tap.after == [nav_x.id]
    # the node after the block depends on the container, not on the lane tails
    assert end.after == [block.id]


def test_parallel_lane_without_resources() -> None:
    register_demo_ops()

    @eks.flow(app="web-app", platform="web")
    def free(s: eks.Session) -> None:
        with eks.parallel():
            first = s.ctx.record("demo.use", {"value": 1})
            second = s.ctx.record("demo.use", {"value": 2})
            s.record_op("ui.nav", {"route": "/x"})
        assert node_of(first) != node_of(second)

    (block,) = eks.plan(free).nodes
    assert block.branches is not None
    first, second, nav = block.branches["body"]
    assert second.after == [first.id]
    assert nav.after == []


def test_container_joins_the_outer_chain_and_stays_the_tail() -> None:
    @eks.flow(app="web-app", platform="web")
    def joined(s: eks.Session) -> None:
        cond = s.record_op("ui.exists", TAP)
        with eks.when(cond):
            s.record_op("ui.tap", TAP)
            s.record_op("ui.nav", {"route": "/inner"})
        s.record_op("ui.nav", {"route": "/after"})

    exists, when, after = eks.plan(joined).nodes
    assert when.after == [exists.id]
    assert when.branches is not None
    tap, inner = when.branches["then"]
    # containment orders the first child after its container
    assert tap.after == []
    assert inner.after == [tap.id]
    assert after.after == [when.id]


def test_reference_to_an_enclosing_container_raises(eks_registry: Registry) -> None:
    # a container's value exists only after its body ran, and its Merkle hash covers its children
    register_demo_ops()
    ctx = TraceContext(kind="flow", name="enclosing", registry=eks_registry)
    with tracing(ctx), ctx.container("ctl.parallel", parallel=True) as block:
        with pytest.raises(TraceError, match="enclosing container"):
            ctx.record("demo.use", {"value": block})
        ctx.record("demo.use", {"value": 1})
    (container,) = ctx.finalize().nodes
    assert container.branches is not None
    assert [node.op for node in container.branches["body"]] == ["demo.use"]


def test_finalizers_run_by_order_then_registration_and_record_nodes() -> None:
    calls: list[str] = []

    def finalizer(name: str) -> Any:
        def run(ctx: TraceContext) -> None:
            calls.append(name)
            assert len(ctx.scope().nodes) >= 1
            ctx.record("media.source", {"uri": f"file:{name}.mp4"}, id_key=f"final:{name}")
            if name == "b":
                ctx.on_finalize(finalizer("late"), order=-5)
        return run

    @eks.flow(app="web-app", platform="web")
    def finalized(s: eks.Session) -> None:
        s.ctx.on_finalize(finalizer("b"), order=1)
        s.ctx.on_finalize(finalizer("a"), order=0)
        s.ctx.on_finalize(finalizer("c"), order=0)
        s.ctx.on_finalize(finalizer("d"), order=-1)
        s.record_op("ui.nav", {"route": "/x"})

    plan = eks.plan(finalized)
    assert calls == ["d", "a", "c", "b", "late"]
    assert [node.args.get("uri") for node in plan.nodes[1:]] == [
        "file:d.mp4", "file:a.mp4", "file:c.mp4", "file:b.mp4", "file:late.mp4",
    ]
    assert all(node.hash for node in plan.nodes)


def test_finalize_requires_the_root_scope(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="open", registry=eks_registry)
    with tracing(ctx), ctx.container("ctl.parallel", parallel=True), pytest.raises(TraceError, match="still open"):
        ctx.finalize()


def test_finalizer_leaving_a_scope_open_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="leaky", registry=eks_registry)

    held: list[Any] = []

    def leaky(inner: TraceContext) -> None:
        # keep the context manager alive: a collected generator would close its scope
        held.append(inner.container("ctl.parallel", parallel=True))
        held[-1].__enter__()

    ctx.on_finalize(leaky)
    with tracing(ctx), pytest.raises(TraceError, match="still open"):
        ctx.finalize()


def test_mismatched_scope_nesting_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="nesting", registry=eks_registry)
    with tracing(ctx):
        cond = ctx.record("media.source", {"uri": "file:a.mp4"})
        outer = ctx.container("ctl.when", {"cond": cond}, branch="then")
        outer.__enter__()
        inner = ctx.container("ctl.parallel", parallel=True)
        inner.__enter__()
        with pytest.raises(TraceError, match="mismatched scope nesting"):
            outer.__exit__(None, None, None)


def test_unknown_branch_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="branches", registry=eks_registry)
    with tracing(ctx):
        cond = ctx.record("media.source", {"uri": "file:a.mp4"})
        with pytest.raises(TraceError, match="branches"), ctx.container("ctl.when", {"cond": cond}, branch="maybe"):
            pass


def test_reopened_branch_continues_its_chain(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="reopen", registry=eks_registry)
    lease = ctx.resource("lease.web", app="web-app", session="main")
    refs: list[Ref[Any]] = []
    with tracing(ctx):
        with ctx.container("ctl.parallel", parallel=False) as block:
            refs.append(ctx.record("ui.tap", TAP, uses=[lease]))
        with ctx.branch(node_of(block), "body"):
            refs.append(ctx.record("ui.tap", TAP, uses=[lease]))
        with pytest.raises(TraceError, match="not a container"), ctx.branch("n0000000000", "body"):
            pass
    (container,) = ctx.finalize().nodes
    assert container.branches is not None
    first, second = container.branches["body"]
    assert second.after == [first.id]
    assert [first.id, second.id] == [node_of(ref) for ref in refs]


def test_container_in_a_parallel_scope_joins_the_lanes_its_body_uses() -> None:
    @eks.flow(app="web-app", platform="web")
    def joined_lanes(s: eks.Session) -> None:
        with eks.parallel():
            s.record_op("ui.tap", TAP)
            cond = s.record_op("api.get", {"path": "/api/flag"})
            with eks.when(cond):
                s.record_op("ui.tap", TAP)
            s.record_op("ui.nav", {"route": "/c"})
            s.record_op("api.get", {"path": "/api/after"})

    (block,) = eks.plan(joined_lanes).nodes
    assert block.branches is not None
    tap_a, api_flag, when, nav_c, api_after = block.branches["body"]
    # the when waits for the lease tail before it (and for its condition) ...
    assert when.after == [tap_a.id, api_flag.id]
    assert when.branches is not None
    (tap_b,) = when.branches["then"]
    assert tap_b.uses == tap_a.uses
    # ... and later lease ops wait for the when, so tap B never overlaps tap A or nav C
    assert nav_c.after == [when.id]
    # the backend lane is untouched by the when body
    assert api_after.after == [api_flag.id]


def test_runtime_each_in_a_parallel_scope_joins_the_lease_lane() -> None:
    @eks.flow(app="web-app", platform="web")
    def looped(s: eks.Session) -> None:
        with eks.parallel():
            rows = s.record_op("api.get", {"path": "/api/rows"})
            with eks.each(rows.items) as row:
                s.record_op("ui.nav", {"route": f"/rows/{row.id}"})
            s.record_op("ui.tap", TAP)

    (block,) = eks.plan(looped).nodes
    assert block.branches is not None
    rows, loop, tap = block.branches["body"]
    assert loop.op == "ctl.each"
    assert loop.after == [rows.id]
    assert tap.after == [loop.id]


def test_otherwise_branch_lanes_join_the_when() -> None:
    @eks.flow(app="web-app", platform="web")
    def branched(s: eks.Session) -> None:
        with eks.parallel():
            s.record_op("api.get", {"path": "/api/a"})
            s.record_op("ui.nav", {"route": "/a"})
            cond = s.record_op("ui.exists", TAP)
            with eks.when(cond):
                s.record_op("ui.tap", TAP)
            with eks.otherwise():
                s.record_op("api.get", {"path": "/api/else"})
            s.record_op("api.get", {"path": "/api/b"})
            s.record_op("ui.nav", {"route": "/b"})

    (block,) = eks.plan(branched).nodes
    assert block.branches is not None
    api_a, nav_a, exists, when, api_b, nav_b = block.branches["body"]
    assert when.after == [api_a.id, exists.id]
    assert api_b.after == [when.id]
    assert nav_b.after == [when.id]
    assert nav_a.id in exists.after


def test_literal_each_copies_with_a_lane_container_chain_in_a_parallel_scope() -> None:
    @eks.flow(app="web-app", platform="web")
    def copied(s: eks.Session) -> None:
        with eks.parallel():
            s.record_op("ui.nav", {"route": "/start"})
            with eks.each(["a", "b"]) as item:
                cond = s.record_op("api.get", {"path": f"/api/{item}"})
                with eks.when(cond):
                    s.record_op("ui.tap", TAP)
            s.record_op("ui.nav", {"route": "/end"})

    (block,) = eks.plan(copied).nodes
    assert block.branches is not None
    start, get_0, when_0, get_1, when_1, end = block.branches["body"]
    assert (get_0.args["path"], get_1.args["path"]) == ("/api/a", "/api/b")
    assert when_0.after == [start.id, get_0.id]
    assert get_1.after == [get_0.id]
    assert when_1.after == [when_0.id, get_1.id]
    assert end.after == [when_1.id]


def test_reopening_a_container_after_its_lane_moved_on_raises(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="flow", name="moved", registry=eks_registry)
    lease = ctx.resource("lease.web", app="web-app", session="main")
    with tracing(ctx), ctx.container("ctl.parallel", parallel=True):
        cond = ctx.record("media.source", {"uri": "file:a.mp4"})
        with ctx.container("ctl.when", {"cond": cond}, branch="then") as when:
            ctx.record("api.get", {"path": "/x"})
        ctx.record("ui.tap", TAP, uses=[lease])
        with pytest.raises(TraceError, match="was recorded on that lane after the container"), \
                ctx.branch(node_of(when), "else"):
            ctx.record("ui.tap", TAP, uses=[lease])


def test_sibling_containers_on_different_lanes_stay_independent() -> None:
    @eks.flow(app="web-app", platform="web")
    def siblings(s: eks.Session) -> None:
        seen = s.record_op("ui.exists", TAP)
        ok = s.record_op("api.get", {"path": "/api/flag"})
        with eks.parallel():
            with eks.when(seen):
                s.record_op("ui.tap", TAP)
            with eks.when(ok):
                s.record_op("api.get", {"path": "/api/then"})
            s.record_op("ui.nav", {"route": "/lease"})
            s.record_op("api.get", {"path": "/api/backend"})
        s.record_op("ui.nav", {"route": "/end"})

    plan = eks.plan(siblings)
    seen, ok, block, end = plan.nodes
    assert block.branches is not None
    lease_when, backend_when, nav, api = block.branches["body"]
    # each when waits only for its condition, not for the sibling container
    assert lease_when.after == [seen.id]
    assert backend_when.after == [ok.id]
    # later nodes wait for the container that drives their lane
    assert nav.after == [lease_when.id]
    assert api.after == [backend_when.id]
    assert end.after == [block.id]


def test_container_with_an_empty_body_takes_the_free_lane(eks_registry: Registry) -> None:
    register_demo_ops()
    ctx = TraceContext(kind="flow", name="empty", registry=eks_registry)
    with tracing(ctx), ctx.container("ctl.parallel", parallel=True):
        cond = ctx.record("media.source", {"uri": "file:a.mp4"})
        free = ctx.record("demo.use", {"value": 1})
        with ctx.container("ctl.when", {"cond": cond}, branch="then") as when:
            pass
        after = ctx.record("demo.use", {"value": 2})
    assert ctx.node(node_of(when)).after == [node_of(free), node_of(cond)]
    assert ctx.node(node_of(after)).after == [node_of(when)]
