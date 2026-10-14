from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from test_engine_scheduler import Rig, register_probes, web_flow

import eks
from eks import by, t
from eks_harness.config import Config
from eks_harness.daemon.context import AppContext
from eks_harness.db import Database
from eks_harness.engine.model import FakeProfile
from eks_harness.engine.sandbox import SandboxLimits, SandboxRunner, SubPlan
from eks_harness.engine.scheduler import SchedulerConfig
from eks_harness.engine.values import value_digest
from eks_harness.ir.hashing import digest, resource_id
from eks_harness.ir.plan import Node, Plan, Resource, SessionSpec
from eks_harness.ir.registry import Registry, default_registry


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        register_probes()
        yield chosen
    finally:
        chosen.restore(state)


@pytest.fixture
def make_rig(ctx: AppContext, db: Database, config: Config, tmp_path: Path) -> Callable[..., Rig]:
    def build(plan: Any, **kwargs: Any) -> Rig:
        kwargs.setdefault("sandbox", SandboxRunner(limits=SandboxLimits(timeout_seconds=120),
                                                   scratch=tmp_path / "sandbox"))
        return Rig(ctx=ctx, db=db, config=config, tmp_path=tmp_path, plan=plan, **kwargs)

    return build


FAKES = FakeProfile(families=["*"], force=["overlay.annotate"])


def dynamic_nodes(plan: Plan) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == "ctl.dynamic"]


def rows_plan(rows: list[Any], name: str = "rows") -> Plan:
    def body(s: eks.Session) -> Any:
        found = s.record_op("probe.echo", {"value": rows})

        @eks.dynamic
        def per_row(items=found, label="row"):
            taps = [s.ui.tap(by.test_id(label).nth(index)) for index in range(len(items))]
            return {"taps": taps, "count": len(items)}

        return {"rows": per_row}

    return web_flow(body, name=name)


def test_dynamic_subplans_run_and_resolve_their_outputs(make_rig: Callable[..., Rig]) -> None:
    plan = rows_plan([{"id": 1}, {"id": 2}, {"id": 3}])
    (dynamic,) = dynamic_nodes(plan)
    rig = make_rig(plan, fakes=FAKES)
    outcome = rig.run()
    assert outcome.status == "passed", outcome.reason
    (expansion,) = [record for record in rig.expansions() if record.kind == "dynamic"]
    assert expansion.node_id == dynamic.id and expansion.instance == ""
    sub = Plan.model_validate(expansion.detail["plan"])
    assert sub.name == f"{plan.name}~{dynamic.id}"
    fn = dynamic.args["fn"]
    assert isinstance(fn, dict) and expansion.detail["fn"] == fn["hash"]
    assert expansion.detail["inputs"] == value_digest({"items": [{"id": 1}, {"id": 2}, {"id": 3}], "label": "row"})
    taps = [node.id for node in sub.nodes]
    assert len(taps) == 3
    for tap in taps:
        record = rig.node(tap, dynamic.id)
        assert record.status == "passed" and record.op == "ui.tap" and record.parent_id == dynamic.id
    output = rig.node(dynamic.id).output
    assert output == {"taps": [None, None, None], "count": 3}
    assert outcome.outputs == {"rows": output}

    def nested(s: eks.Session) -> Any:
        groups = s.record_op("probe.echo", {"value": [["a"], ["b", "c"]]})
        with eks.each(groups) as group:
            @eks.dynamic
            def per_group(members=group):
                return [s.ui.tap(by.test_id(member)) for member in members]

        return groups

    plan = web_flow(nested, name="nested")
    loop = plan.nodes[1]
    (inner,) = dynamic_nodes(plan)
    rig = make_rig(plan, fakes=FAKES)
    assert rig.run().status == "passed"
    for index, count in ((0, 1), (1, 2)):
        instance = f"{loop.id}#{index}"
        child = f"{instance}/{inner.id}"
        records = [record for record in rig.nodes() if record.instance == child]
        assert len(records) == count and all(record.status == "passed" for record in records)
        assert rig.node(inner.id, instance).output == [None] * count
    expansions = [(record.instance, record.kind) for record in rig.expansions()]
    assert (f"{loop.id}#0", "dynamic") in expansions and (f"{loop.id}#1", "dynamic") in expansions


def test_pure_subplan_nodes_hit_the_cache_across_iterations(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        rows = s.record_op("probe.echo", {"value": ["same", "same"]})
        with eks.each(rows) as row:
            @eks.dynamic
            def annotated(text=row):
                shot = s.shot(text)
                return eks.annotate(shot, [eks.overlay.highlight("#total")])

        return rows

    plan = web_flow(body)
    loop = plan.nodes[1]
    (inner,) = dynamic_nodes(plan)
    rig = make_rig(plan, fakes=FAKES)
    outcome = rig.run()
    assert outcome.status == "passed", outcome.reason
    assert outcome.cache.stored == 1 and outcome.cache.hits == 1
    annotations = [record for record in rig.nodes() if record.op == "overlay.annotate"]
    assert [record.instance for record in annotations] == [f"{loop.id}#0/{inner.id}", f"{loop.id}#1/{inner.id}"]
    assert [record.cache for record in annotations] == ["stored", "hit"]
    assert annotations[0].cache_key == annotations[1].cache_key
    assert annotations[0].output == annotations[1].output
    outputs = [rig.node(inner.id, f"{loop.id}#{index}").output for index in range(2)]
    assert outputs[0] == outputs[1] and isinstance(outputs[0], dict)


class Tampering(SandboxRunner):
    def __init__(self, change: Callable[[Plan, Plan, Node], None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.change = change

    def dynamic(self, plan: Plan, node: Node, *, instance: str, inputs: Mapping[str, JsonValue], tree: Path | None,
                plugins: bool, digests: Mapping[str, str], session: SessionSpec | None = None) -> SubPlan:
        found = super().dynamic(plan, node, instance=instance, inputs=inputs, tree=tree, plugins=plugins,
                                digests=digests, session=plan.session)
        self.change(found.plan, plan, node)
        return found


def failure_of(rig: Rig, node: Node) -> tuple[str, str, Any]:
    outcome = rig.run()
    assert outcome.status == "failed", outcome
    record = rig.node(node.id)
    assert record.error is not None
    return record.error.code, record.error.message, record.error.detail


def test_invalid_subplans_are_refused(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    plan = rows_plan([{"id": 1}, {"id": 2}, {"id": 3}])
    (dynamic,) = dynamic_nodes(plan)
    small = SandboxRunner(limits=SandboxLimits(timeout_seconds=120, max_nodes=2), scratch=tmp_path / "small")
    code, message, detail = failure_of(make_rig(plan, fakes=FAKES, sandbox=small), dynamic)
    assert code == "subplan_invalid" and "3 nodes, more than the limit of 2" in message
    assert isinstance(detail["issues"], list) and detail["issues"][0]["code"] == "subplan.nodes"

    def foreign(sub: Plan, parent: Plan, node: Node) -> None:
        lease = next(resource for resource in sub.resources if resource.kind == "lease.web")
        sub.resources = [resource if resource is not lease else lease.model_copy(update={"app": "other-app"})
                         for resource in sub.resources]

    tampered = Tampering(foreign, limits=SandboxLimits(timeout_seconds=120), scratch=tmp_path / "foreign")
    code, message, _detail = failure_of(make_rig(plan, fakes=FAKES, sandbox=tampered), dynamic)
    assert code == "subplan_invalid" and "differs from the parent plan" in message

    def missing(sub: Plan, parent: Plan, node: Node) -> None:
        stranger = Resource(id="rzzzzzzz", kind="lease.web", app="web-app", session="main")
        sub.resources.append(stranger)
        sub.nodes[0].uses = [stranger.id]

    tampered = Tampering(missing, limits=SandboxLimits(timeout_seconds=120), scratch=tmp_path / "missing")
    code, message, _detail = failure_of(make_rig(plan, fakes=FAKES, sandbox=tampered), dynamic)
    assert code == "subplan_invalid" and "rzzzzzzz, which the parent plan does not hold" in message

    def nested(sub: Plan, parent: Plan, node: Node) -> None:
        sub.nodes.append(node.model_copy(update={"id": "n000000nest"}))

    tampered = Tampering(nested, limits=SandboxLimits(timeout_seconds=120), scratch=tmp_path / "nested")
    code, message, _detail = failure_of(
        make_rig(plan, fakes=FAKES, sandbox=tampered, settings=SchedulerConfig(parallelism=1, max_depth=1)), dynamic)
    assert code == "subplan_invalid" and "deeper than the limit of 1" in message

    main = plan.session
    assert main is not None
    take_session = main.model_copy(update={"key": "take0"})
    lease = Resource(id=resource_id("lease.web", {"app": "web-app", "session": "take0"}), kind="lease.web",
                     app="web-app", session="take0")
    source, dyn = plan.nodes
    take = Node(id="n0000000t00", op="score.take", args={"session": take_session.model_dump(mode="json")},
                uses=[lease.id], out="media", branches={"body": [dyn]}, after=[source.id], hash=dyn.hash)
    in_take = plan.model_copy(update={"nodes": [source, take], "resources": [*plan.resources, lease],
                                      "outputs": None})
    tampered = Tampering(pin_first, limits=SandboxLimits(timeout_seconds=120), scratch=tmp_path / "pinned")
    rig = make_rig(in_take, fakes=FAKES, sandbox=tampered)
    outcome = rig.run()
    assert outcome.status == "failed"
    record = rig.node(dyn.id)
    assert record.error is not None and record.error.code == "subplan_invalid"
    assert "carries a pin inside a score.take" in record.error.message


def pin_first(sub: Plan, parent: Plan, node: Node) -> None:
    sub.nodes[0].pin = t("1s")


def crafted(node: Node, source: str) -> Node:
    text = textwrap.dedent(source).lstrip("\n")
    name = text.split("(", 1)[0].removeprefix("def ").strip()
    fn = {"name": name, "source": text, "hash": digest(text.encode("utf-8")), "params": ["n"], "freevars": {},
          "globals": []}
    return node.model_copy(update={"args": {"fn": fn, "inputs": {"n": 1}}})


def crafted_plan(source: str) -> tuple[Plan, Node]:
    plan = rows_plan([1], name="crafted")
    first, dynamic = plan.nodes
    replaced = crafted(dynamic, source)
    return plan.model_copy(update={"nodes": [first, replaced]}), replaced


def test_sandbox_failures_surface_with_their_codes(make_rig: Callable[..., Rig], tmp_path: Path) -> None:
    plan, node = crafted_plan("""
        def boom(n=1):
            return [n][5]
    """)
    code, message, detail = failure_of(make_rig(plan, fakes=FAKES), node)
    assert code == "sandbox_error" and "IndexError" in str(detail.get("type")) + message
    assert detail["code"] == "user_error"

    plan, node = crafted_plan("""
        def scribble(n=1):
            eks.tracing.builtins.open("written.txt", "w")
            return [n]
    """)
    code, message, detail = failure_of(make_rig(plan, fakes=FAKES), node)
    assert code == "sandbox_violation" and detail["code"] == "violation"

    plan, node = crafted_plan("""
        def spin(n=1):
            while True:
                n = n + 1
    """)
    quick = SandboxRunner(limits=SandboxLimits(timeout_seconds=1), scratch=tmp_path / "quick")
    code, message, detail = failure_of(make_rig(plan, fakes=FAKES, sandbox=quick), node)
    assert code == "sandbox_timeout" and detail["code"] == "limit"


def test_subplan_ops_without_executors_fail(make_rig: Callable[..., Rig]) -> None:
    plan = rows_plan([{"id": 1}])
    (dynamic,) = dynamic_nodes(plan)
    rig = make_rig(plan)
    rig.deps.choose = lambda spec: None if spec.name == "ui.tap" else rig.choose(spec)
    outcome = rig.run()
    assert outcome.status == "failed"
    taps = [record for record in rig.nodes() if record.op == "ui.tap"]
    assert len(taps) == 1
    (tap,) = taps
    assert tap.instance == dynamic.id and tap.status == "failed"
    assert tap.error is not None and tap.error.code == "executor_missing"
    assert rig.node(dynamic.id).status == "failed"
    assert rig.node(dynamic.id).error is not None
