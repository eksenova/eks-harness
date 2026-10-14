from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from eks.tracing import TraceError, register_plugin_root
from eks.workspace import plan_spec
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry

PACKAGE = "crit_outside_stamp"
FLOW = textwrap.dedent(f'''
    import eks

    import {PACKAGE}


    @eks.flow(app="web-app", now="2026-01-15T09:00+03:00", timezone="Europe/Istanbul", locale="tr-TR")
    def stamped(s: eks.Session):
        s.ui.nav("/")
        {PACKAGE}.stamp(s)
''')
PACKAGE_SOURCE = textwrap.dedent('''
    import eks
    from eks import t


    def stamp(s):
        s.overlay.image("#logo", "assets/logo.png", hold=t("1s"))
        s.ui.tap("#ok")
''')
APP = '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n'


def make_tree(root: Path, flow: str = FLOW) -> Path:
    (root / ".harness" / "flows").mkdir(parents=True)
    (root / ".harness" / "project.toml").write_text('[project]\nid = "outside/tree"\n', encoding="utf-8")
    (root / "web-app" / ".harness").mkdir(parents=True)
    (root / "web-app" / ".harness" / "app.toml").write_text(APP, encoding="utf-8")
    (root / ".harness" / "flows" / "stamped.py").write_text(flow, encoding="utf-8")
    return root


@pytest.fixture
def package(tmp_path: Path) -> Iterator[Path]:
    site = tmp_path / "sitepkg"
    (site / PACKAGE / "assets").mkdir(parents=True)
    (site / PACKAGE / "__init__.py").write_text(PACKAGE_SOURCE, encoding="utf-8")
    sys.path.insert(0, str(site))
    try:
        yield site / PACKAGE
    finally:
        sys.path.remove(str(site))
        sys.modules.pop(PACKAGE, None)


def planned(tree: Path) -> Plan:
    return plan_spec(".harness/flows/stamped.py", cwd=tree, plugins=False)


def test_code_outside_the_tree_plans_the_same_from_any_tree_location(tmp_path: Path, package: Path,
                                                                    eks_registry: Registry) -> None:
    state = eks_registry.snapshot()
    near = planned(make_tree(tmp_path / "loc1" / "dsl"))
    eks_registry.restore(state)
    far = planned(make_tree(tmp_path / "loc2" / "deeper" / "nest" / "dsl"))
    assert near.to_json() == far.to_json()
    assert near.hash == far.hash
    sources = [node for node, _ancestors in near.walk() if node.op == "media.source"]
    assert [node.args["uri"] for node in sources] == [f"pkg:{PACKAGE}/assets/logo.png"]
    srcs = {node.src for node, _ancestors in near.walk() if node.op in ("overlay.show", "ui.tap")}
    assert srcs == {f"pkg:{PACKAGE}/__init__.py:7", f"pkg:{PACKAGE}/__init__.py:8"}
    assert not any(".." in (node.src or "") for node, _ancestors in near.walk())


def test_plugin_code_is_addressed_by_plugin_id(tmp_path: Path, package: Path, eks_registry: Registry,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eks.tracing._PLUGIN_ROOTS", {})
    register_plugin_root("acme.stamps", package)
    plan = planned(make_tree(tmp_path / "dsl"))
    (source,) = [node for node, _ancestors in plan.walk() if node.op == "media.source"]
    assert source.args["uri"] == "plugin:acme.stamps/assets/logo.png"
    assert {node.src for node, _ancestors in plan.walk() if node.op == "ui.tap"} == {"plugin:acme.stamps/__init__.py:8"}


def test_media_paths_that_leave_the_tree_are_rejected(tmp_path: Path, eks_registry: Registry) -> None:
    flow = textwrap.dedent('''
        import eks


        @eks.edit(fps=30, size=(1080, 1920))
        def escape(e: eks.Edit):
            e.track("main").clip(e.media("../../../outside/clip.mp4"))
    ''')
    tree = make_tree(tmp_path / "dsl", flow)
    with pytest.raises(TraceError, match="outside the tree"):
        planned(tree)
