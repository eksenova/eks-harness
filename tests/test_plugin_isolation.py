from __future__ import annotations

import importlib
import sys
import textwrap
import time
from pathlib import Path

import pytest

from eks_harness.config import Config
from eks_harness.plugins import PluginHost

MANIFEST = """
    [plugin]
    id = "acme.shared"
    name = "Shared"
    version = "1.0.0"
    api = "1"

    [[contributes.seeder]]
    id = "scenario"
    entry = "isolated_pkg.seed:Scenario"
"""

SEED = """
    from isolated_pkg.values import LABEL
    from . import values


    class Scenario:
        def __init__(self, context):
            self.context = context

        def scenarios(self):
            import isolated_pkg.values as lazy
            from isolated_pkg import values as same

            assert lazy is values and same is values
            return [LABEL, lazy.LABEL]
"""


def write_tree(tree: Path, label: str, *, pre_trust: bool = True, package_name: str = "isolated_pkg") -> Path:
    plugin = tree / ".harness" / "plugins" / "shared"
    package = plugin / package_name
    package.mkdir(parents=True)
    (plugin / "harness-plugin.toml").write_text(textwrap.dedent(MANIFEST).replace("isolated_pkg", package_name),
                                                encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "seed.py").write_text(textwrap.dedent(SEED).replace("isolated_pkg", package_name), encoding="utf-8")
    (package / "values.py").write_text(f"LABEL = {label!r}\n", encoding="utf-8")
    trust = '\n[plugins]\ntrust = ["acme.shared"]\n' if pre_trust else ""
    (tree / ".harness" / "project.toml").write_text(f'[project]\nid = "acme/{tree.name}"\n{trust}', encoding="utf-8")
    return plugin


@pytest.fixture
def host(config: Config, tmp_path: Path) -> PluginHost:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    return PluginHost(config.paths, config, builtin_root=builtin, include_packages=False)


@pytest.fixture(autouse=True)
def clean_bare_modules(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield
    for name in [n for n in sys.modules if n == "isolated_pkg" or n.startswith("isolated_pkg.")]:
        del sys.modules[name]


def scenarios(host: PluginHost, tree: Path) -> list[str]:
    return host.load(host.contribution("seeder", "scenario", tree), tree).scenarios()


def test_two_trees_with_the_same_module_names_load_their_own_code(host: PluginHost, tmp_path: Path) -> None:
    tree_a, tree_b = tmp_path / "worktree-a", tmp_path / "worktree-b"
    write_tree(tree_a, "from-a")
    write_tree(tree_b, "from-b")
    assert scenarios(host, tree_a) == ["from-a", "from-a"]
    assert scenarios(host, tree_b) == ["from-b", "from-b"]
    assert "isolated_pkg" not in sys.modules
    assert not any(str(tree_a) in entry or str(tree_b) in entry for entry in sys.path)


def test_edited_plugin_code_loads_as_a_new_generation(host: PluginHost, tmp_path: Path) -> None:
    tree = tmp_path / "repo"
    plugin = write_tree(tree, "first")
    assert scenarios(host, tree) == ["first", "first"]
    time.sleep(0.01)
    (plugin / "isolated_pkg" / "values.py").write_text("LABEL = 'second'\n", encoding="utf-8")
    assert scenarios(host, tree) == ["second", "second"]


def test_bare_imports_of_a_plugin_file_share_the_scoped_module(host: PluginHost, tmp_path: Path) -> None:
    tree_a, tree_b = tmp_path / "a", tmp_path / "b"
    plugin_a = write_tree(tree_a, "from-a")
    write_tree(tree_b, "from-b")
    scenarios(host, tree_a)
    scenarios(host, tree_b)
    sys.path.insert(0, str(plugin_a))
    bare = importlib.import_module("isolated_pkg.values")
    assert bare.LABEL == "from-a"
    scope = next(s for s in host.activate_imports(tree_a) if s.plugin_id == "acme.shared")
    assert bare is sys.modules[scope.scoped_name("isolated_pkg.values")]
    assert bare.__spec__.name == scope.scoped_name("isolated_pkg.values")


def test_bare_import_without_a_path_entry_resolves_when_one_tree_provides_it(host: PluginHost,
                                                                             tmp_path: Path) -> None:
    tree = tmp_path / "only"
    write_tree(tree, "only-tree", package_name="isolated_solo_pkg")
    host.activate_imports(tree)
    try:
        assert importlib.import_module("isolated_solo_pkg.values").LABEL == "only-tree"
    finally:
        for name in [n for n in sys.modules if n.startswith("isolated_solo_pkg")]:
            del sys.modules[name]


def test_identical_content_approved_in_one_worktree_is_trusted_in_another(host: PluginHost, tmp_path: Path) -> None:
    tree_a, tree_b, tree_c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    write_tree(tree_a, "same", pre_trust=False)
    write_tree(tree_b, "same", pre_trust=False)
    write_tree(tree_c, "different", pre_trust=False)
    assert host.get("acme.shared", tree_b).state == "pending"
    host.trust("acme.shared", tree_a)
    assert host.get("acme.shared", tree_a).state == "active"
    assert host.get("acme.shared", tree_b).state == "active"
    assert host.get("acme.shared", tree_c).state == "pending"
