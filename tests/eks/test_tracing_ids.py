import hashlib
import importlib.util
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import eks
from eks.planning import trace
from eks.tracing import TraceContext, normalize_call_text, tracing
from eks_harness.ir.hashing import derive_id
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry

TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


def short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def by_route(plan: Plan) -> dict[str, Node]:
    return {str(node.args["route"]): node for node, _ancestors in plan.walk() if node.op == "ui.nav"}


def load(path: Path, source: str) -> ModuleType:
    # the same module name on every load, so the registry sees a reload of the same entry
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("p4_ids_edited_flow", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan_file(path: Path, source: str, registry: Registry) -> Plan:
    module = load(path, source)
    return trace(registry.entry_for(module.edited), registry=registry, tree=path.parent)


def test_same_flow_planned_twice_is_byte_identical() -> None:
    @eks.flow(app="web-app", platform="web", as_="harman-admin")
    def twice(s: eks.Session, branch: str = "Kadıköy") -> dict[str, Any]:
        rows = s.record_op("api.get", {"path": "/api/tenant-branches"})
        s.record_op("ui.nav", {"route": f"/tenant/invoice/list?branchId={rows.items.find(name=branch).id}"})
        with eks.when(s.record_op("ui.exists", TAP)):
            s.record_op("ui.tap", TAP)
        with eks.each(["a", "b"]) as item:
            s.record_op("ui.nav", {"route": f"/r/{item}"})
        return {"rows": rows}

    first = eks.plan(twice)
    second = eks.plan(twice)
    assert first.to_json().encode() == second.to_json().encode()
    assert first.hash == second.hash
    assert first.hash.startswith("sha256:")
    assert [node.id for node, _ in first.walk()] == [node.id for node, _ in second.walk()]
    assert all(node.hash for node, _ in first.walk())


def test_identical_statements_get_different_stable_ids() -> None:
    @eks.flow(app="web-app", platform="web")
    def repeated(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)
        s.record_op("ui.tap", TAP)
        for _ in range(2):
            s.record_op("ui.tap", TAP)

    first = [node.id for node in eks.plan(repeated).nodes]
    second = [node.id for node in eks.plan(repeated).nodes]
    assert len(set(first)) == 4
    assert first == second


def test_node_id_follows_the_documented_key() -> None:
    @eks.flow(app="web-app", platform="web")
    def keyed(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)
        s.record_op(  "ui.tap",
                    TAP  )

    first, second = eks.plan(keyed).nodes
    # the call expression is normalized to its tokens joined by single spaces, comments dropped
    assert normalize_call_text('s.record_op(  "ui.tap",\n   TAP  )  # x') == 's . record_op ( "ui.tap" , TAP )'
    segment = f"tests/eks/test_tracing_ids.py:{keyed.__qualname__}:{short('s . record_op ( \"ui.tap\" , TAP )')}"
    key = f"flow:keyed/{segment}/ui.tap"
    assert first.id == derive_id("n", key + "#0")
    # spacing differs between the two calls, so they share the key and differ by occurrence
    assert second.id == derive_id("n", key + "#1")


def test_ids_survive_blank_lines_comments_and_unrelated_statements(tmp_path: Path, eks_registry: Registry) -> None:
    path = tmp_path / "edited.py"
    original = """\
        import eks

        TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


        @eks.flow(app="web-app", platform="web")
        def edited(s: eks.Session):
            s.record_op("ui.nav", {"route": "/a"})
            s.record_op("ui.tap", TAP)
            s.record_op("ui.nav", {"route": "/b"})
    """
    cosmetic = """\
        # a comment before everything
        import eks


        TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}



        @eks.flow(app="web-app", platform="web")
        def edited(s: eks.Session):
            # first we navigate

            s.record_op("ui.nav", {"route": "/a"})   # trailing comment


            s.record_op("ui.tap", TAP)
            s.record_op("ui.nav", {"route": "/b"})
    """
    extended = """\
        import eks

        TAP = {"target": {"by": "text", "args": {"text": "Kaydet"}}}


        @eks.flow(app="web-app", platform="web")
        def edited(s: eks.Session):

            s.record_op("ui.nav", {"route": "/a"})
            # an unrelated statement with different text
            s.record_op("ui.tap", TAP)
            s.record_op("ui.nav", {"route": "/inserted"})
            s.record_op("ui.nav", {"route": "/b"})
    """
    before = plan_file(path, original, eks_registry)
    after_cosmetic = plan_file(path, cosmetic, eks_registry)
    after_extension = plan_file(path, extended, eks_registry)

    assert [node.id for node in after_cosmetic.nodes] == [node.id for node in before.nodes]
    assert [node.hash for node in after_cosmetic.nodes] == [node.hash for node in before.nodes]
    assert after_cosmetic.hash == before.hash
    assert after_cosmetic.to_json() != before.to_json()  # only src lines differ

    old = before.nodes
    new = {node.id: node for node in after_extension.nodes}
    assert all(node.id in new for node in old)
    # nodes before the change keep their hashes, the node after it moves in its lane
    assert new[old[0].id].hash == old[0].hash
    assert new[old[1].id].hash == old[1].hash
    assert new[old[2].id].hash != old[2].hash
    assert after_extension.hash != before.hash
    assert by_route(after_extension)["/b"].after == [by_route(after_extension)["/inserted"].id]


def test_ids_do_not_depend_on_how_the_tree_is_spelled(
    tmp_path: Path, eks_registry: Registry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    flows = tree / ".harness" / "flows"
    flows.mkdir(parents=True)
    path = flows / "spelled.py"
    source = """\
        import eks


        @eks.flow(app="web-app", platform="web")
        def edited(s: eks.Session):
            s.record_op("ui.nav", {"route": "/a"})
            s.record_op("ui.tap", {"target": {"by": "text", "args": {"text": "Kaydet"}}})
    """
    link = tmp_path / "link"
    link.symlink_to(tree, target_is_directory=True)

    module = load(path, source)
    absolute = trace(eks_registry.entry_for(module.edited), registry=eks_registry, tree=tree)
    monkeypatch.chdir(tmp_path)
    relative = trace(eks_registry.entry_for(module.edited), registry=eks_registry, tree=Path("tree"))
    through_link = trace(eks_registry.entry_for(module.edited), registry=eks_registry, tree=link)
    linked_module = load(link / ".harness" / "flows" / "spelled.py", source)
    linked_file = trace(eks_registry.entry_for(linked_module.edited), registry=eks_registry, tree=tree)

    for other in (relative, through_link, linked_file):
        assert [node.id for node in other.nodes] == [node.id for node in absolute.nodes]
        assert [node.src for node in other.nodes] == [node.src for node in absolute.nodes]
        assert other.source == absolute.source
        assert other.base == absolute.base
        assert other.hash == absolute.hash
        assert other.to_json() == absolute.to_json()
    assert absolute.base == ".harness/flows"
    assert absolute.nodes[0].src == ".harness/flows/spelled.py:6"


def test_key_suffix_extends_the_op_segment(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="seed", name="suffixed", registry=eks_registry)
    with tracing(ctx):
        ctx.record("media.source", {"uri": "file:a.mp4"}, key_suffix="alpha")
        ctx.record("media.source", {"uri": "file:b.mp4"}, key_suffix="beta")
        ctx.record("media.source", {"uri": "file:c.mp4"}, key_suffix="alpha", id_key="explicit")
    first, second, third = ctx.finalize().nodes
    assert first.id == derive_id("n", "seed:suffixed/lib/media.source:alpha#0")
    assert second.id == derive_id("n", "seed:suffixed/lib/media.source:beta#0")
    # id_key replaces the whole call chain, so the suffix does not apply
    assert third.id == derive_id("n", "seed:suffixed/explicit#0")


def test_changing_an_argument_changes_only_that_id(tmp_path: Path, eks_registry: Registry) -> None:
    path = tmp_path / "edited.py"
    source = """\
        import eks


        @eks.flow(app="web-app", platform="web")
        def edited(s: eks.Session):
            s.record_op("ui.nav", {"route": "/a"})
            s.record_op("ui.tap", {"target": {"by": "text", "args": {"text": "%s"}}})
            s.record_op("ui.nav", {"route": "/b"})
    """
    before = plan_file(path, source % "Kaydet", eks_registry)
    after = plan_file(path, source % "Sil", eks_registry)
    old_ids = [node.id for node in before.nodes]
    new_ids = [node.id for node in after.nodes]
    assert old_ids[0] == new_ids[0]
    assert old_ids[1] != new_ids[1]
    assert old_ids[2] == new_ids[2]
    assert before.nodes[0].hash == after.nodes[0].hash
    assert before.nodes[2].hash != after.nodes[2].hash


def tap_twice_helper(s: eks.Session) -> None:
    s.record_op("ui.tap", TAP)


def test_helper_called_from_two_call_sites_gets_two_ids() -> None:
    @eks.flow(app="web-app", platform="web")
    def helper_calls(s: eks.Session) -> None:
        tap_twice_helper(s)
        tap_twice_helper(s=s)

    @eks.flow(app="web-app", platform="web")
    def direct(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP)

    first, second = eks.plan(helper_calls).nodes
    assert first.id != second.id
    flow_segment = f"tests/eks/test_tracing_ids.py:{helper_calls.__qualname__}:{short('tap_twice_helper ( s )')}"
    helper_segment = f"tests/eks/test_tracing_ids.py:tap_twice_helper:{short('s . record_op ( \"ui.tap\" , TAP )')}"
    assert first.id == derive_id("n", f"flow:helper_calls/{flow_segment}>{helper_segment}/ui.tap#0")
    # the helper's own source line is the innermost frame, so it is the node's src
    assert first.src is not None
    assert first.src.startswith("tests/eks/test_tracing_ids.py:")
    assert first.src == second.src
    assert eks.plan(direct).nodes[0].id not in {first.id, second.id}


def test_id_key_overrides_the_call_chain() -> None:
    @eks.flow(app="web-app", platform="web")
    def overridden(s: eks.Session) -> None:
        s.record_op("ui.tap", TAP, id_key="resource/tenant:harman")
        s.record_op("ui.nav", {"route": "/x"}, id_key="resource/tenant:harman")

    first, second = eks.plan(overridden).nodes
    assert first.id == derive_id("n", "flow:overridden/resource/tenant:harman#0")
    assert second.id == derive_id("n", "flow:overridden/resource/tenant:harman#1")


def test_nodes_without_user_frames_use_lib() -> None:
    @eks.flow(app="web-app", platform="web", as_="harman-admin")
    def library_nodes(s: eks.Session) -> None:
        def late(ctx: TraceContext) -> None:
            ctx.record("ui.tap", TAP, uses=[s.resources["lease"]])

        s.ctx.on_finalize(late)
        s.record_op("ui.nav", {"route": "/x"})

    plan = eks.plan(library_nodes)
    session_as, nav, late = plan.nodes
    assert session_as.op == "session.as"
    assert session_as.id == derive_id("n", "flow:library_nodes/lib/session.as#0")
    assert session_as.src is None
    # finalizers run after the entry function returned, so their nodes have no user frames either
    assert late.id == derive_id("n", "flow:library_nodes/lib/ui.tap#0")
    assert late.src is None
    assert nav.src is not None


def test_direct_context_without_entry_uses_lib(eks_registry: Registry) -> None:
    ctx = TraceContext(kind="seed", name="direct", registry=eks_registry)
    with tracing(ctx):
        ctx.record("media.source", {"uri": "file:a.mp4"})
        ctx.record("media.source", {"uri": "file:b.mp4"})
    first, second = ctx.finalize().nodes
    assert first.id == derive_id("n", "seed:direct/lib/media.source#0")
    assert second.id == derive_id("n", "seed:direct/lib/media.source#1")


def test_ids_inside_containers_use_the_branch_scope() -> None:
    @eks.flow(app="web-app", platform="web")
    def scoped(s: eks.Session) -> None:
        with eks.when(s.record_op("ui.exists", TAP)):
            s.record_op("ui.tap", TAP, id_key="inner")

    _exists, when = eks.plan(scoped).nodes
    assert when.branches is not None
    (inner,) = when.branches["then"]
    assert inner.id == derive_id("n", f"{when.id}/then/inner#0")


def test_call_site_reports_the_innermost_user_frame() -> None:
    sites: list[Any] = []

    @eks.flow(app="web-app", platform="web")
    def located(s: eks.Session) -> None:
        sites.append(s.ctx.call_site())

    eks.plan(located)
    (site,) = sites
    assert site.file == "tests/eks/test_tracing_ids.py"
    assert site.qualname == located.__qualname__
    assert site.text == "s . ctx . call_site ( )"
    assert site.src() == f"tests/eks/test_tracing_ids.py:{located.__code__.co_firstlineno + 2}"
