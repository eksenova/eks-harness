from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import textwrap
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

import eks
from eks import discovery, workspace
from eks.discovery import (
    DiscoveryError,
    builtins_registry,
    forget_file,
    forget_tree,
    load_plugins,
    module_name,
    sync_tree,
    tree_registry,
)
from eks.workspace import plan_spec, seed_personas, workspace_registry
from eks_harness.config import Config
from eks_harness.engine import registries
from eks_harness.engine.registries import (
    TREE_CAPACITY,
    HubRegistries,
    TreeRegistry,
    plugin_digest,
    tree_code_digest,
)
from eks_harness.ir.plan import Plan, Requirement
from eks_harness.ir.registry import (
    Registry,
    RegistryError,
    base_registry,
    contributing,
    default_registry,
    using_registry,
)
from eks_harness.plugins.host import PluginHost
from eks_harness.plugins.trust import content_hash


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


@pytest.fixture
def host(config: Config, tmp_path: Path) -> PluginHost:
    builtin = tmp_path / "no-builtins"
    builtin.mkdir(exist_ok=True)
    return PluginHost(config.paths, config, builtin_root=builtin, include_packages=False)


@pytest.fixture
def hub(host: PluginHost) -> HubRegistries:
    return HubRegistries(host)


ACME_OPS = """
    import eks


    @eks.op("acme", out="json", description="{description}")
    class Ping(eks.Args):
        target: str = "self"
"""

SEED = """
    import eks


    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"


    @eks.seed("{name}", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
    def {name}(d: eks.Data):
        d.member("{persona}")
"""

EXTRA_SEED = """
    import eks


    @eks.seed("extra", backend="eksenapi", now="2026-01-15T09:00+03:00")
    def extra(d: eks.Data):
        return None
"""

FLOW = """
    import eks


    @eks.flow(app="web-app", platform="web")
    def visit(s: eks.Session):
        s.record_op("ui.nav", {"route": "/kasa"})
"""


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def bump(*paths: Path) -> None:
    stamp = time.time() + 5
    for path in paths:
        os.utime(path, (stamp, stamp))


def write_project(tree: Path, *, trust: list[str]) -> Path:
    listed = ", ".join(f'"{name}"' for name in trust)
    project = write(tree / ".harness" / "project.toml",
                    f'[project]\nid = "acme/{tree.name}"\n\n[plugins]\ntrust = [{listed}]\n')
    bump(project)
    return project


def make_tree(tree: Path, *, trust: list[str] | None = None, seed: bool = True) -> Path:
    write_project(tree, trust=trust or [])
    write(tree / "web-app" / ".harness" / "app.toml", '[app]\nname = "web-app"\nplatforms = ["web"]\n')
    if seed:
        write(tree / ".harness" / "seeds" / "standard.py",
              SEED.format(name="standard", persona="harman-admin"))
    return tree


def write_manifest(root: Path, plugin_id: str, version: str, modules: list[str]) -> None:
    listed = ", ".join(f'"{module}"' for module in modules)
    write(root / "harness-plugin.toml", f"""
        [plugin]
        id = "{plugin_id}"
        name = "Test {plugin_id}"
        version = "{version}"
        api = "1"
        modules = [{listed}]
    """)


def write_plugin(root: Path, plugin_id: str = "acme", *, version: str = "1.2.0", description: str = "first",
                 module: str = "acme_ops") -> Path:
    write_manifest(root, plugin_id, version, [module])
    write(root / f"{module}.py", ACME_OPS.format(description=description))
    bump(root / f"{module}.py", root)
    return root


EXTRA_OPS = """
    import eks


    @eks.op("acme", out="json", description="Only in the second module")
    class Extra(eks.Args):
        target: str = "self"
"""


def acme_tree(tree: Path, *, description: str = "first", version: str = "1.2.0") -> Path:
    make_tree(tree, trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", description=description, version=version)
    bump(tree / ".harness" / "plugins")
    return tree


def plugin_records(registry: Registry, plugin_id: str) -> list[tuple[Registry, str, str]]:
    return [key for key in discovery._PLUGIN_MODULES if key[0] is registry and key[1] == plugin_id]


def working(tree: Path | None, *, plugins: bool = True) -> Registry:
    return tree_registry(tree, plugins=plugins, isolated=True)


def scoped_names(prefix: str) -> list[str]:
    return [name for name in sys.modules if name.partition(".")[0] == prefix]


def file_keys(registry: Registry) -> list[str]:
    return [key[1] for key in discovery._FILES if key[0] is registry]


def flow_plan() -> Plan:
    @eks.flow(app="web-app", platform="web")
    def visit(s: eks.Session) -> None:
        s.record_op("ui.nav", {"route": "/kasa"})

    return eks.plan(visit)


def test_s1_ops_registered_outside_a_tree_stay_out_of_isolated_registries(tmp_path: Path, hub: HubRegistries) -> None:
    tree = make_tree(tmp_path / "shop")

    @eks.op("leak", out="json", description="Registered by the host process outside any tree")
    class Ping(eks.Args):
        target: str = "self"

    assert default_registry().has_op("leak.ping")
    assert tree_registry(tree).has_op("leak.ping")
    isolated = tree_registry(tree, isolated=True)
    assert not isolated.has_op("leak.ping")
    assert isolated.has_op("ui.tap")
    entry = hub.for_tree(tree)
    assert entry.registry is not isolated
    assert hub.for_tree(tree).registry is entry.registry
    assert not entry.registry.has_op("leak.ping")
    assert entry.registry.has_op("ui.tap")
    assert entry.registry.has_op("api.get")
    assert entry.registry.entry("seed", "standard").name == "standard"
    assert not builtins_registry().has_op("leak.ping")
    assert tree_registry(tree, isolated=True) is isolated
    assert tree_registry(tree) is not isolated


def test_builtins_registry_follows_the_builtin_specs(registry: Registry) -> None:
    first = builtins_registry()
    assert builtins_registry() is first
    assert first.has_op("ui.tap")
    assert {spec.provenance.plugin for spec in first.ops()} == {"eks"}
    state = registry.snapshot()
    probe = type("Probe", (eks.Args,), {"__module__": "eks_harness.zz_registry_probe"})
    eks.op("zzprobe", description="A built-in op added after start")(probe)
    assert registry.op("zzprobe.probe").provenance.plugin == "eks"
    rebuilt = builtins_registry()
    assert rebuilt is not first
    assert rebuilt.has_op("zzprobe.probe")
    registry.restore(state)
    assert not builtins_registry().has_op("zzprobe.probe")


def test_s2_untrusted_or_removed_plugins_are_pruned(tmp_path: Path, hub: HubRegistries) -> None:
    tree = acme_tree(tmp_path / "shop")
    entry = hub.for_tree(tree)
    assert entry.registry.op("acme.ping").provenance.plugin == "acme"
    assert set(entry.plugins) == {"acme"}
    assert entry.plugins["acme"].version == "1.2.0"
    assert plugin_records(working(tree), "acme")
    assert not default_registry().has_op("acme.ping")
    scope = entry.registry.op("acme.ping").args.__module__.partition(".")[0]
    assert scoped_names(scope)

    write_project(tree, trust=[])
    hub.invalidate(tree)
    untrusted = hub.for_tree(tree)
    assert untrusted.registry is not entry.registry
    assert not untrusted.registry.has_op("acme.ping")
    assert untrusted.plugins == {}
    assert untrusted.generation > entry.generation
    assert plugin_records(working(tree), "acme") == []
    assert not working(tree).has_op("acme.ping")
    assert scoped_names(scope) == []
    assert entry.registry.op("acme.ping").description == "first"
    assert entry.plugin("acme") is not None

    write_project(tree, trust=["acme"])
    again = hub.for_tree(tree)
    assert again.registry.has_op("acme.ping")
    assert plugin_records(working(tree), "acme")

    shutil.rmtree(tree / ".harness" / "plugins" / "acme")
    bump(tree / ".harness" / "plugins")
    hub.invalidate(tree)
    removed = hub.for_tree(tree)
    assert not removed.registry.has_op("acme.ping")
    assert plugin_records(working(tree), "acme") == []
    assert again.registry.has_op("acme.ping")
    plan = flow_plan().model_copy(update={"requires": [Requirement(plugin="acme", version="1.2.0")]})
    assert [issue.code for issue in hub.requirement_issues(plan, removed)] == ["plugin.missing"]


def test_load_plugins_prune_keeps_builtin_project_and_active_plugins(
    tmp_path: Path, host: PluginHost
) -> None:
    tree = acme_tree(tmp_path / "shop")
    chosen = Registry()
    chosen.rebase(builtins_registry())
    with using_registry(chosen):
        assert load_plugins(tree, host=host, prune=True) == ["acme:acme_ops"]

        @eks.op("local", description="Project code in the tree")
        class Local(eks.Args):
            pass

        with contributing("ghost", "0.1"):
            @eks.op("ghost", description="A plugin that is not active")
            class Boo(eks.Args):
                pass

        assert chosen.has_op("ghost.boo")
        load_plugins(tree, host=host, prune=True)
    assert chosen.has_op("acme.ping")
    assert chosen.has_op("local.local")
    assert chosen.has_op("ui.tap")
    assert not chosen.has_op("ghost.boo")


def test_s3_deleted_entry_files_disappear(tmp_path: Path, hub: HubRegistries) -> None:
    tree = make_tree(tmp_path / "shop")
    extra = write(tree / ".harness" / "seeds" / "extra.py", EXTRA_SEED)
    entry = hub.for_tree(tree)
    assert {spec.name for spec in entry.registry.entries("seed")} == {"standard", "extra"}
    assert module_name(extra) in file_keys(entry.registry)
    assert module_name(extra) in file_keys(working(tree))
    assert module_name(extra) in sys.modules
    extra.unlink()
    after = hub.for_tree(tree)
    assert {spec.name for spec in after.registry.entries("seed")} == {"standard"}
    assert module_name(extra) not in file_keys(after.registry)
    assert module_name(extra) not in file_keys(working(tree))
    assert module_name(extra) not in file_keys(entry.registry)
    assert module_name(extra) not in sys.modules
    assert after.generation > entry.generation
    assert after.project_digest != entry.project_digest
    assert {spec.name for spec in entry.registry.entries("seed")} == {"standard", "extra"}


def test_sync_tree_drops_deleted_files_of_the_active_registry_only(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    extra = write(tree / ".harness" / "seeds" / "extra.py", EXTRA_SEED)
    flow = write(tree / ".harness" / "flows" / "visit.py", FLOW)
    chosen = tree_registry(tree, isolated=True)
    with using_registry(chosen):
        names = [spec.name for spec in sync_tree(tree, kinds=("seed",))]
        assert sorted(names) == ["extra", "standard"]
        assert not chosen.entries("flow")
        everything = sync_tree(tree)
        assert {spec.name for spec in everything} == {"extra", "standard", "visit"}
        extra.unlink()
        flow.unlink()
        remaining = sync_tree(tree)
    assert [spec.name for spec in remaining] == ["standard"]
    assert [spec.name for spec in chosen.entries()] == ["standard"]
    assert file_keys(chosen) == [module_name(tree / ".harness" / "seeds" / "standard.py")]


def test_s4_discovery_caches_stay_bounded(tmp_path: Path, hub: HubRegistries) -> None:
    trees = [acme_tree(tmp_path / f"t{index:02d}") for index in range(70)]
    entries: list[TreeRegistry] = []
    workers: list[Registry] = []
    for tree in trees:
        entry = hub.for_tree(tree)
        assert seed_personas("standard", registry=entry.registry) == ["harman-admin"]
        assert file_keys(entry.registry)
        worker = working(tree)
        assert plugin_records(worker, "acme")
        assert any(key[0] == id(entry.registry) for key in workspace._SEEDS)
        entries.append(entry)
        workers.append(worker)
    assert len(hub.trees()) == TREE_CAPACITY
    base = base_registry()

    def trees_of(registries: set[Registry]) -> set[Path | None]:
        assert all(registry in discovery._TREE_OF for registry in registries)
        return {discovery._TREE_OF[registry] for registry in registries}

    assert len({key[0] for key in discovery._TREES}) <= 64
    assert len(trees_of({key[0] for key in discovery._FILES} - {base})) <= 64
    assert len(trees_of({key[0] for key in discovery._PLUGIN_MODULES} - {base})) <= 64
    assert len({key[0] for key in workspace._SEEDS}) <= 64
    assert len(discovery._OWNERS) == 64
    for index, entry in enumerate(entries[:6]):
        assert entry.tree is not None
        assert all(key[0] != entry.tree for key in discovery._TREES)
        assert all(root != entry.tree for root in discovery._TREE_OF.values())
        for registry in (entry.registry, workers[index]):
            assert registry not in discovery._TREE_OF
            assert all(key[0] is not registry for key in discovery._FILES)
            assert all(key[0] is not registry for key in discovery._PLUGIN_MODULES)
            assert all(key[0] != id(registry) for key in workspace._SEEDS)
        assert module_name(entry.tree / ".harness" / "seeds" / "standard.py") not in sys.modules
        assert scoped_names(entry.registry.op("acme.ping").args.__module__.partition(".")[0]) == []
    last = entries[-1]
    assert last.tree is not None
    assert any(key[0] == last.tree for key in discovery._TREES)
    assert file_keys(last.registry)
    assert plugin_records(workers[-1], "acme")
    for tree in trees:
        hub.forget(tree)
    assert all(key[0] not in trees for key in discovery._TREES)


def test_bl004_changed_plugin_code_gets_a_new_digest_and_generation(tmp_path: Path, hub: HubRegistries) -> None:
    tree = acme_tree(tmp_path / "shop", description="first")
    first = hub.for_tree(tree)
    same = hub.for_tree(tree)
    assert same.generation == first.generation
    assert same.plugin("acme") == first.plugin("acme")
    assert same.registry is first.registry
    old_op = first.registry.op("acme.ping")
    assert old_op.description == "first"
    old_scope = old_op.args.__module__.partition(".")[0]
    assert scoped_names(old_scope)
    write_plugin(tree / ".harness" / "plugins" / "acme", description="second")
    changed = hub.for_tree(tree)
    assert changed.plugins["acme"].version == "1.2.0"
    assert changed.plugin("acme") != first.plugin("acme")
    assert changed.plugin("acme") == content_hash(tree / ".harness" / "plugins" / "acme")
    assert changed.generation > first.generation
    assert changed.registry is not first.registry
    new_op = changed.registry.op("acme.ping")
    assert new_op.description == "second"
    assert new_op.args.__module__ != old_op.args.__module__
    assert first.registry.op("acme.ping") is old_op
    assert first.registry.op("acme.ping").description == "first"
    assert first.plugin("acme") != changed.plugin("acme")
    assert scoped_names(old_scope) == []
    assert new_op.args.__module__ in sys.modules
    assert changed.project_digest == first.project_digest
    assert changed.roots() == {"acme": tree / ".harness" / "plugins" / "acme"}


def test_bl004_digestless_plugins_are_reimported_when_their_content_changes(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = "eks_engine_registries_pkg_ops"
    builtins_root = tmp_path / "builtins"
    root = write_plugin(builtins_root / "pkg", "pkgfx", version="3.0.0", description="old code", module=module)
    monkeypatch.syspath_prepend(str(root))
    host = PluginHost(config.paths, config, builtin_root=builtins_root, include_packages=False)
    (record,) = [record for record in host.active(None) if record.id == "pkgfx"]
    assert record.digest is None
    assert plugin_digest(record) == content_hash(root)
    hub = HubRegistries(host)
    tree = make_tree(tmp_path / "shop")
    try:
        first = hub.for_tree(tree)
        assert first.plugins["pkgfx"].kind == "builtin"
        assert first.registry.op("acme.ping").description == "old code"
        write(root / f"{module}.py", ACME_OPS.format(description="new code"))
        changed = hub.for_tree(tree)
        assert changed.plugin("pkgfx") != first.plugin("pkgfx")
        assert changed.generation > first.generation
        assert changed.registry.op("acme.ping").description == "new code"
        assert sys.modules[module].__file__ is not None
    finally:
        hub.forget(tree)
        forget_file(root / f"{module}.py")
    assert module not in sys.modules


def rewrite_keeping_stamp(source: Path, body: str) -> None:
    before = source.stat()
    write(source, body)
    assert source.stat().st_size == before.st_size
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))


def test_bl004_a_package_plugin_imported_by_another_registry_is_reimported_at_its_digest(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    module = "eks_engine_registries_pkg_first"
    builtins_root = tmp_path / "builtins"
    root = write_plugin(builtins_root / "pkg", "pkgfx", version="3.0.0", description="old code", module=module)
    source = root / f"{module}.py"
    monkeypatch.syspath_prepend(str(root))
    host = PluginHost(config.paths, config, builtin_root=builtins_root, include_packages=False)
    tree = make_tree(tmp_path / "shop")
    shared = tree_registry(tree)
    try:
        with using_registry(shared):
            load_plugins(tree, host=host)
        assert shared.op("acme.ping").description == "old code"
        assert Path(importlib.util.cache_from_source(str(source))).is_file()
        rewrite_keeping_stamp(source, ACME_OPS.format(description="new code"))
        hub = HubRegistries(host)
        entry = hub.for_tree(tree)
        assert entry.plugin("pkgfx") == content_hash(root)
        assert entry.registry.op("acme.ping").description == "new code"
        again = hub.for_tree(tree)
        assert again is entry
        other = HubRegistries(host).for_tree(tree)
        assert other.plugin("pkgfx") == entry.plugin("pkgfx")
        assert other.registry.op("acme.ping").description == "new code"
        with using_registry(shared):
            load_plugins(tree, host=host)
        assert shared.op("acme.ping").description == "new code"
        hub.forget(tree)
    finally:
        forget_file(source)
        forget_tree(tree)
    assert module not in sys.modules


def test_bl004_a_same_size_edit_in_the_same_second_runs_the_new_code(
    tmp_path: Path, hub: HubRegistries, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    tree = acme_tree(tmp_path / "shop", description="first")
    root = tree / ".harness" / "plugins" / "acme"
    source = root / "acme_ops.py"
    first = hub.for_tree(tree)
    old_op = first.registry.op("acme.ping")
    assert old_op.description == "first"
    assert Path(importlib.util.cache_from_source(str(source))).is_file()
    rewrite_keeping_stamp(source, ACME_OPS.format(description="later"))
    bump(root)
    changed = hub.for_tree(tree)
    assert changed.plugin("acme") == content_hash(root)
    assert changed.plugin("acme") != first.plugin("acme")
    assert changed.generation > first.generation
    assert changed.registry.op("acme.ping").description == "later"
    assert scoped_names(old_op.args.__module__.partition(".")[0]) == []
    assert first.registry.op("acme.ping") is old_op
    hub.forget(tree)


def test_plugin_digest_prefers_the_record_digest(tmp_path: Path, host: PluginHost) -> None:
    tree = acme_tree(tmp_path / "shop")
    (record,) = host.active(tree)
    assert record.digest is not None
    assert plugin_digest(record) == record.digest
    assert record.digest == content_hash(record.root)


def test_tree_code_digest_covers_harness_python_only(tmp_path: Path) -> None:
    first = make_tree(tmp_path / "a")
    second = make_tree(tmp_path / "b")
    write(first / ".harness" / "flows" / "visit.py", FLOW)
    write(second / ".harness" / "flows" / "visit.py", FLOW)
    digest = tree_code_digest(first)
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert tree_code_digest(second) == digest

    write_plugin(first / ".harness" / "plugins" / "acme")
    write(first / ".harness" / "plugins" / "acme" / "more" / "deep.py", "VALUE = 1\n")
    write(first / ".harness" / "flows" / "notes.txt", "not code\n")
    write(first / ".harness" / "flows" / "__pycache__" / "visit.cpython-313.py", "VALUE = 2\n")
    write(first / "src" / "app.py", "VALUE = 3\n")
    write(first / "nested" / ".harness" / "project.toml", '[project]\nid = "acme/nested"\n')
    write(first / "nested" / ".harness" / "flows" / "inner.py", FLOW)
    assert tree_code_digest(first) == digest

    write(first / ".harness" / "flows" / "visit.py", FLOW + "\n# changed\n")
    flows_changed = tree_code_digest(first)
    assert flows_changed != digest
    write(first / ".harness" / "flows" / "visit.py", FLOW)
    assert tree_code_digest(first) == digest

    write(first / ".harness" / "seeds" / "standard.py", SEED.format(name="standard", persona="other"))
    assert tree_code_digest(first) != digest
    write(first / ".harness" / "seeds" / "standard.py", SEED.format(name="standard", persona="harman-admin"))
    assert tree_code_digest(first) == digest

    write(first / "web-app" / ".harness" / "flows" / "app_flow.py", FLOW)
    assert tree_code_digest(first) != digest
    (first / "web-app" / ".harness" / "flows" / "app_flow.py").unlink()
    write(first / ".harness" / "app.py", "VALUE = 4\n")
    assert tree_code_digest(first) != digest


def test_project_digest_change_bumps_the_generation(tmp_path: Path, hub: HubRegistries) -> None:
    tree = make_tree(tmp_path / "shop")
    first = hub.for_tree(tree)
    assert first.project() == tree_code_digest(tree)
    write(tree / ".harness" / "flows" / "visit.py", FLOW)
    second = hub.for_tree(tree)
    assert second.project() != first.project()
    assert second.generation > first.generation
    assert hub.for_tree(tree).generation == second.generation


def test_invalidate_forgets_the_file_and_reloads_new_content(tmp_path: Path, hub: HubRegistries) -> None:
    tree = make_tree(tmp_path / "shop")
    seed = tree / ".harness" / "seeds" / "standard.py"
    entry = hub.for_tree(tree)
    hub.for_tree(tree, plugins=False)
    assert seed_personas("standard", registry=entry.registry) == ["harman-admin"]
    assert module_name(seed) in file_keys(entry.registry)
    write(seed, SEED.format(name="renamed", persona="kasiyer"))
    hub.invalidate(tree, seed)
    assert hub.trees() == []
    assert module_name(seed) not in file_keys(entry.registry)
    assert module_name(seed) not in file_keys(working(tree))
    assert working(tree).entries("seed") == []
    assert all(key[0] != id(entry.registry) for key in workspace._SEEDS)
    assert [spec.name for spec in entry.registry.entries("seed")] == ["standard"]
    assert seed_personas("standard", registry=entry.registry) == ["harman-admin"]
    fresh = hub.for_tree(tree)
    assert fresh.registry is not entry.registry
    assert [spec.name for spec in fresh.registry.entries("seed")] == ["renamed"]
    assert seed_personas("renamed", registry=fresh.registry) == ["kasiyer"]

    write(seed, SEED.format(name="standard", persona="muhasebeci"))
    hub.invalidate(tree, Path(".harness/seeds/standard.py"))
    assert [spec.name for spec in hub.for_tree(tree).registry.entries("seed")] == ["standard"]


def test_forget_drops_every_cache_of_the_tree(tmp_path: Path, hub: HubRegistries) -> None:
    tree = acme_tree(tmp_path / "shop")
    entry = hub.for_tree(tree)
    worker = working(tree)
    seed_personas("standard", registry=entry.registry)
    assert plugin_records(worker, "acme")
    scoped = entry.registry.op("acme.ping").args.__module__
    hub.forget(tree)
    assert hub.trees() == []
    assert all(key[0] != entry.tree for key in discovery._TREES)
    assert all(root != entry.tree for root in discovery._TREE_OF.values())
    assert file_keys(entry.registry) == []
    assert file_keys(worker) == []
    assert plugin_records(worker, "acme") == []
    assert all(key[0] != id(entry.registry) for key in workspace._SEEDS)
    assert scoped not in sys.modules
    again = hub.for_tree(tree)
    assert again.registry is not entry.registry
    assert again.registry.has_op("acme.ping")
    assert again.generation > entry.generation


def test_forget_file_of_a_plugin_module_reimports_it(tmp_path: Path, host: PluginHost) -> None:
    tree = acme_tree(tmp_path / "shop")
    chosen = tree_registry(tree, isolated=True)
    with using_registry(chosen):
        load_plugins(tree, host=host, prune=True)
    first = chosen.op("acme.ping")
    forget_file(tree / ".harness" / "plugins" / "acme" / "acme_ops.py")
    assert not chosen.has_op("acme.ping")
    assert plugin_records(chosen, "acme") == []
    assert first.args.__module__ not in sys.modules
    with using_registry(chosen):
        load_plugins(tree, host=host, prune=True)
    again = chosen.op("acme.ping")
    assert again is not first
    assert again.args is not first.args
    forget_tree(tree)


def test_lru_capacity_and_flags(tmp_path: Path, host: PluginHost) -> None:
    hub = HubRegistries(host, capacity=2)
    first = acme_tree(tmp_path / "a")
    second = make_tree(tmp_path / "b")
    third = make_tree(tmp_path / "c")
    hub.for_tree(first)
    hub.for_tree(second)
    hub.for_tree(first)
    hub.for_tree(third)
    assert [(entry.tree, entry.plugins_enabled) for entry in hub.trees()] == [
        (first.resolve(), True), (third.resolve(), True)]

    bare = hub.for_tree(first, plugins=False)
    assert bare.plugins_enabled is False
    assert bare.plugins == {}
    assert bare.roots() == {}
    assert bare.plugin("acme") is None
    assert not bare.registry.has_op("acme.ping")
    assert bare.registry.has_op("ui.tap")
    assert [(entry.tree, entry.plugins_enabled) for entry in hub.trees()] == [
        (third.resolve(), True), (first.resolve(), False)]
    full = hub.for_tree(first)
    assert full.registry is not bare.registry
    assert working(first) is not working(first, plugins=False)
    assert full.registry.has_op("acme.ping")
    assert full.plugin("acme") is not None
    assert len(hub.trees()) == 2


def test_for_tree_without_a_tree(hub: HubRegistries) -> None:
    entry = hub.for_tree(None)
    assert entry.tree is None
    assert entry.project() is None
    assert entry.registry.has_op("ui.tap")
    assert entry.registry is not tree_registry(None, isolated=True)
    again = hub.for_tree(None)
    assert again.generation == entry.generation
    assert again.registry is entry.registry


def test_requirement_issues(tmp_path: Path, hub: HubRegistries) -> None:
    tree = acme_tree(tmp_path / "shop")
    entry = hub.for_tree(tree)
    plan = flow_plan()

    def issues(*requires: Requirement) -> list[tuple[str, str]]:
        changed = plan.model_copy(update={"requires": list(requires)})
        return [(issue.code, issue.message) for issue in hub.requirement_issues(changed, entry)]

    assert issues() == []
    assert issues(Requirement(plugin="acme", version="1.2.0")) == []
    assert issues(Requirement(plugin="eks", version="0.0.0")) == []
    missing = Requirement(plugin="ghost", version="0.1.0")
    other = Requirement(plugin="acme", version="9.9.9")
    found = issues(missing, other)
    assert [code for code, _message in found] == ["plugin.missing", "plugin.version"]
    validated = eks.validate(plan.model_copy(update={"requires": [missing, other]}), registry=entry.registry)
    expected = [(issue.code, issue.message) for issue in validated.issues
                if issue.code in ("plugin.missing", "plugin.version")]
    assert found == expected
    severities = {issue.severity for issue in hub.requirement_issues(
        plan.model_copy(update={"requires": [missing]}), entry)}
    assert severities == {"error"}


def test_plan_spec_with_a_given_registry(tmp_path: Path, hub: HubRegistries) -> None:
    tree = make_tree(tmp_path / "shop")
    flow = write(tree / ".harness" / "flows" / "visit.py", FLOW)
    entry = hub.for_tree(tree)
    root = tree.resolve()
    assert (root, True, False) not in discovery._TREES
    plan = plan_spec(f"{flow}::visit", registry=entry.registry)
    assert plan.name == "visit"
    assert [node.op for node in plan.nodes] == ["ui.nav"]
    assert entry.registry.entry("flow", "visit").name == "visit"
    assert (root, True, False) not in discovery._TREES
    assert (root, False, False) not in discovery._TREES
    assert not default_registry().entries("flow") or all(
        spec.name != "visit" or spec.file != str(flow) for spec in default_registry().entries("flow"))
    assert not any(spec.name == "visit" for spec in tree_registry(tree).entries("flow"))
    default_plan = plan_spec(f"{flow}::visit")
    assert default_plan.hash == plan.hash


def test_workspace_registry_isolated(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    extra = write(tree / ".harness" / "seeds" / "extra.py", EXTRA_SEED)

    @eks.op("leak", description="Outside every tree")
    class Outside(eks.Args):
        pass

    chosen = workspace_registry(tree, plugins=False, isolated=True)
    assert chosen is tree_registry(tree, plugins=False, isolated=True)
    assert not chosen.has_op("leak.outside")
    assert {spec.name for spec in chosen.entries("seed")} == {"standard", "extra"}
    extra.unlink()
    assert {spec.name for spec in workspace_registry(tree, plugins=False, isolated=True).entries("seed")} == {
        "standard"}
    shared = workspace_registry(tree, plugins=False)
    assert shared is tree_registry(tree, plugins=False)
    assert shared.has_op("leak.outside")
    assert workspace_registry(None, plugins=False) is default_registry()
    assert workspace_registry(None, plugins=False, isolated=True) is tree_registry(None, plugins=False, isolated=True)


def test_s2_a_module_removed_from_the_manifest_is_pruned(tmp_path: Path, hub: HubRegistries) -> None:
    tree = make_tree(tmp_path / "shop", trust=["acme"])
    root = tree / ".harness" / "plugins" / "acme"
    write_manifest(root, "acme", "1.2.0", ["acme_ops", "acme_more"])
    write(root / "acme_ops.py", ACME_OPS.format(description="first"))
    write(root / "acme_more.py", EXTRA_OPS)
    bump(root / "acme_ops.py", root / "acme_more.py", root)
    first = hub.for_tree(tree)
    assert first.registry.has_op("acme.ping")
    assert first.registry.has_op("acme.extra")
    assert sorted(key[2] for key in plugin_records(working(tree), "acme")) == ["acme_more", "acme_ops"]
    old_more = first.registry.op("acme.extra").args.__module__
    assert old_more in sys.modules

    write_manifest(root, "acme", "1.2.0", ["acme_ops"])
    (root / "acme_more.py").unlink()
    bump(root / "harness-plugin.toml", root, tree / ".harness" / "plugins")
    changed = hub.for_tree(tree)
    assert changed.plugin("acme") != first.plugin("acme")
    assert changed.generation > first.generation
    assert changed.registry.has_op("acme.ping")
    assert not changed.registry.has_op("acme.extra")
    assert not working(tree).has_op("acme.extra")
    assert [key[2] for key in plugin_records(working(tree), "acme")] == ["acme_ops"]
    assert old_more not in sys.modules
    assert first.registry.has_op("acme.extra")


def test_load_plugins_prune_drops_modules_an_active_plugin_no_longer_lists(tmp_path: Path, host: PluginHost) -> None:
    tree = make_tree(tmp_path / "shop", trust=["acme"])
    root = tree / ".harness" / "plugins" / "acme"
    write_manifest(root, "acme", "1.2.0", ["acme_ops", "acme_more"])
    write(root / "acme_ops.py", ACME_OPS.format(description="first"))
    write(root / "acme_more.py", EXTRA_OPS)
    chosen = working(tree)
    with using_registry(chosen):
        assert load_plugins(tree, host=host, prune=True) == ["acme:acme_ops", "acme:acme_more"]
    assert chosen.has_op("acme.extra")
    write_manifest(root, "acme", "1.2.0", [])
    bump(root / "harness-plugin.toml", root, tree / ".harness" / "plugins")
    with using_registry(chosen):
        assert load_plugins(tree, host=host, prune=True) == []
    assert not chosen.has_op("acme.ping")
    assert not chosen.has_op("acme.extra")
    assert plugin_records(chosen, "acme") == []
    forget_tree(tree)


def test_published_registries_keep_their_specs_when_files_are_forgotten(tmp_path: Path, hub: HubRegistries) -> None:
    tree = acme_tree(tmp_path / "shop")
    seed = tree / ".harness" / "seeds" / "standard.py"
    entry = hub.for_tree(tree)
    op = entry.registry.op("acme.ping")
    forget_file(seed)
    forget_file(tree / ".harness" / "plugins" / "acme" / "acme_ops.py")
    assert entry.registry.op("acme.ping") is op
    assert [spec.name for spec in entry.registry.entries("seed")] == ["standard"]
    assert not working(tree).has_op("acme.ping")
    assert working(tree).entries("seed") == []
    again = hub.for_tree(tree)
    assert again.generation == entry.generation
    assert again.registry is entry.registry
    assert working(tree).has_op("acme.ping")


def test_a_generation_survives_discovery_eviction(tmp_path: Path, hub: HubRegistries) -> None:
    tree = make_tree(tmp_path / "shop")
    entry = hub.for_tree(tree)
    assert discovery._TREE_OF[entry.registry] == tree.resolve()
    forget_tree(tree)
    assert entry.registry not in discovery._TREE_OF
    again = hub.for_tree(tree)
    assert again is entry
    assert discovery._TREE_OF[entry.registry] == tree.resolve()
    assert [spec.name for spec in entry.registry.entries("seed")] == ["standard"]


def test_project_digest_rehashes_only_changed_stamps(
    tmp_path: Path, hub: HubRegistries, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = make_tree(tmp_path / "shop")
    calls: list[int] = []
    original = registries._digest_files

    def counting(files: Sequence[tuple[str, Path, os.stat_result]]) -> str:
        calls.append(len(files))
        return original(files)

    monkeypatch.setattr(registries, "_digest_files", counting)
    first = hub.for_tree(tree)
    assert calls == [1]
    assert first.project() == tree_code_digest(tree)
    assert hub.for_tree(tree).project() == first.project()
    assert hub.for_tree(tree, plugins=False).project() == first.project()
    assert calls == [1, 1]
    write(tree / ".harness" / "flows" / "visit.py", FLOW)
    changed = hub.for_tree(tree)
    assert calls == [1, 1, 2]
    assert changed.project() != first.project()
    assert hub.for_tree(tree).project() == changed.project()
    assert calls == [1, 1, 2]
    assert changed.project() == tree_code_digest(tree)
    hub.invalidate(tree)
    assert hub.for_tree(tree).project() == changed.project()
    assert calls == [1, 1, 2, 2, 2]


def reference_walk(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, _files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in discovery.SKIP_DIRS and not d.startswith(".")
                         and not (here / d / ".harness" / "project.toml").is_file())
        found.append(here)
    return found


def test_walk_matches_the_os_walk_rules(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    for folder in ("b/inner", "a/deep/er", "node_modules/pkg", ".hidden/x", "__pycache__", "c"):
        (tree / folder).mkdir(parents=True, exist_ok=True)
    write(tree / "nested" / ".harness" / "project.toml", '[project]\nid = "acme/nested"\n')
    write(tree / "nested" / "child" / ".harness" / "flows" / "x.py", FLOW)
    write(tree / "a" / "deep" / ".harness" / "flows" / "y.py", FLOW)
    (tree / "link").symlink_to(tree / "a", target_is_directory=True)
    root = tree.resolve()
    assert list(discovery._walk(root)) == reference_walk(root)
    assert discovery._harness_parents(root) == [
        path for path in reference_walk(root) if (path / ".harness").is_dir()]
    assert [item.path for item in discovery.discover(root)] == [
        root / ".harness" / "seeds" / "standard.py", root / "a" / "deep" / ".harness" / "flows" / "y.py"]
    assert list(discovery._walk(root / "missing")) == []


def test_for_tree_runs_trees_in_parallel_threads(tmp_path: Path, hub: HubRegistries) -> None:
    trees = [acme_tree(tmp_path / f"t{index}") for index in range(4)]
    results: dict[Path, TreeRegistry] = {}
    errors: list[Exception] = []

    def run(tree: Path) -> None:
        try:
            for _ in range(3):
                results[tree] = hub.for_tree(tree)
        except (DiscoveryError, RegistryError, OSError) as error:
            errors.append(error)

    threads = [threading.Thread(target=run, args=(tree,)) for tree in trees]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    for tree in trees:
        entry = results[tree]
        assert entry.registry.has_op("acme.ping")
        assert [spec.name for spec in entry.registry.entries("seed")] == ["standard"]
        assert hub.for_tree(tree) is entry


def age(root: Path) -> None:
    stamp = time.time() - 60
    for current, dirs, _files in os.walk(root):
        for name in dirs:
            os.utime(Path(current) / name, (stamp, stamp), follow_symlinks=False)
    os.utime(root, (stamp, stamp))


def test_the_scan_cache_relists_only_changed_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = make_tree(tmp_path / "shop")
    for folder in ("a/one", "a/two", "b", "c/deep/er"):
        (tree / folder).mkdir(parents=True, exist_ok=True)
    root = tree.resolve()
    age(root)
    listed: list[str] = []
    original = discovery._children

    def counting(directory: str) -> tuple[list[str], bool] | None:
        listed.append(directory)
        return original(directory)

    monkeypatch.setattr(discovery, "_children", counting)
    assert list(discovery._walk(root)) == reference_walk(root)
    assert len(listed) == len(reference_walk(root))
    listed.clear()
    assert list(discovery._walk(root)) == reference_walk(root)
    assert listed == []

    write(root / "b" / "app" / ".harness" / "flows" / "z.py", FLOW)
    assert list(discovery._walk(root)) == reference_walk(root)
    assert str(root / "b") in listed
    assert str(root / "a") not in listed
    assert root / "b" / "app" in discovery._harness_parents(root)
    assert root / "b" / "app" / ".harness" / "flows" / "z.py" in [item.path for item in discovery.discover(root)]

    age(root)
    list(discovery._walk(root))
    listed.clear()
    write(root / "b" / "app" / ".harness" / "project.toml", '[project]\nid = "acme/app"\n')
    assert list(discovery._walk(root)) == reference_walk(root)
    assert root / "b" / "app" not in list(discovery._walk(root))
    assert str(root / "b" / "app") in listed

    age(root)
    list(discovery._walk(root))
    listed.clear()
    shutil.rmtree(root / "c" / "deep")
    assert list(discovery._walk(root)) == reference_walk(root)
    assert listed == [str(root / "c")]
    assert set(discovery._SCANS[root]) == {str(path) for path in reference_walk(root)} | {str(root / "b" / "app")}


HELPED_SEED = """
    import eks
    from _shop_cast import NAME


    @eks.resource("member", persona=True)
    class Member(eks.Resource):
        role: str = "admin"


    @eks.seed("standard", backend="eksenapi", now="2026-01-15T09:00+03:00", seed=2026)
    def standard(d: eks.Data):
        d.member(NAME)
"""

HELPER_MODULES = ("_shop_cast", "_shop_roster")


@pytest.fixture
def helped_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    tree = make_tree(tmp_path / "shop", seed=False)
    seeds = tree / ".harness" / "seeds"
    write(seeds / "_shop_roster.py", 'NAME = "first"\n')
    write(seeds / "_shop_cast.py", "from _shop_roster import NAME\n")
    write(seeds / "standard.py", HELPED_SEED)
    for name in HELPER_MODULES:
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(seeds))
    try:
        yield tree
    finally:
        for name in HELPER_MODULES:
            sys.modules.pop(name, None)
        forget_tree(tree)


def test_a_changed_project_helper_is_imported_again_in_the_next_generation(
    helped_tree: Path, hub: HubRegistries
) -> None:
    roster = helped_tree / ".harness" / "seeds" / "_shop_roster.py"
    first = hub.for_tree(helped_tree)
    assert seed_personas("standard", registry=first.registry) == ["first"]
    old_cast = sys.modules["_shop_cast"]
    assert hub.for_tree(helped_tree) is first
    assert sys.modules["_shop_cast"] is old_cast
    write(roster, 'NAME = "second"\n')
    bump(roster)
    second = hub.for_tree(helped_tree)
    assert second.project() == tree_code_digest(helped_tree)
    assert second.project() != first.project()
    assert second.generation > first.generation
    assert seed_personas("standard", registry=second.registry) == ["second"]
    assert seed_personas("standard", registry=first.registry) == ["first"]
    assert sys.modules["_shop_cast"] is not old_cast
    assert hub.for_tree(helped_tree) is second
    assert seed_personas("standard", registry=hub.for_tree(helped_tree, plugins=False).registry) == ["second"]
    write(roster, 'NAME = "third"\n')
    bump(roster)
    hub.invalidate(helped_tree, roster)
    assert seed_personas("standard", registry=hub.for_tree(helped_tree).registry) == ["third"]
    assert seed_personas("standard", registry=hub.for_tree(helped_tree, plugins=False).registry) == ["third"]


def test_a_same_size_helper_edit_in_the_same_second_runs_the_new_code(
    helped_tree: Path, hub: HubRegistries, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    roster = helped_tree / ".harness" / "seeds" / "_shop_roster.py"
    first = hub.for_tree(helped_tree)
    assert seed_personas("standard", registry=first.registry) == ["first"]
    assert Path(importlib.util.cache_from_source(str(roster))).is_file()
    rewrite_keeping_stamp(roster, 'NAME = "fifth"\n')
    changed = hub.for_tree(helped_tree)
    assert changed.generation > first.generation
    assert seed_personas("standard", registry=changed.registry) == ["fifth"]


def test_a_deleted_project_helper_fails_the_seed_instead_of_keeping_old_code(
    helped_tree: Path, hub: HubRegistries
) -> None:
    seeds = helped_tree / ".harness" / "seeds"
    first = hub.for_tree(helped_tree)
    assert seed_personas("standard", registry=first.registry) == ["first"]
    (seeds / "_shop_roster.py").unlink()
    bump(seeds)
    with pytest.raises(ImportError):
        hub.for_tree(helped_tree)
    assert working(helped_tree).entries("seed") == []
    write(seeds / "_shop_roster.py", 'NAME = "back"\n')
    assert seed_personas("standard", registry=hub.for_tree(helped_tree).registry) == ["back"]
