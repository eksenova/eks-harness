from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eks.discovery import DiscoveryError, load_plugins
from eks_harness.config import Config
from eks_harness.ir.registry import Provenance, Registry
from eks_harness.plugins.host import PluginHost
from eks_harness.plugins.manifest import ManifestError, load_manifest, parse_manifest

ACME_OPS = """
    import eks

    @eks.op("acme", out="json", description="Ping the acme service")
    class Ping(eks.Args):
        target: str = "self"

    @eks.macro()
    def wave(s):
        return None
"""


def write_plugin(root: Path, plugin_id: str, *, version: str = "1.2.0", modules: str = '["acme_ops"]',
                 files: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness-plugin.toml").write_text(textwrap.dedent(f"""
        [plugin]
        id = "{plugin_id}"
        name = "Test {plugin_id}"
        version = "{version}"
        api = "1"
        modules = {modules}
    """), encoding="utf-8")
    for name, body in (files or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def write_tree(tree: Path, *, trust: list[str], disable: list[str] | None = None) -> Path:
    harness = tree / ".harness"
    harness.mkdir(parents=True, exist_ok=True)
    plugins = f"[plugins]\ntrust = {trust!r}\n".replace("'", '"')
    if disable:
        plugins += f"disable = {disable!r}\n".replace("'", '"')
    (harness / "project.toml").write_text(f'[project]\nid = "acme/{tree.name}"\n{plugins}', encoding="utf-8")
    return tree


@pytest.fixture
def host(config: Config, tmp_path: Path) -> PluginHost:
    builtin = tmp_path / "no-builtins"
    builtin.mkdir()
    return PluginHost(config.paths, config, builtin_root=builtin, include_packages=False)


def test_trusted_repo_plugin_modules_register_with_provenance(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", files={"acme_ops.py": ACME_OPS})
    assert load_plugins(tree, host=host) == ["acme:acme_ops"]
    ping = eks_registry.op("acme.ping")
    assert ping.provenance == Provenance("acme", "1.2.0")
    assert ping.args.__module__.endswith(".acme_ops")
    assert ping.args.__module__.startswith("_eks_harness_plugin_acme_")
    macro = eks_registry.macro("acme.wave")
    assert macro.provenance == Provenance("acme", "1.2.0")
    assert eks_registry.versions()["acme"] == "1.2.0"


def test_loading_again_is_idempotent(tmp_path: Path, host: PluginHost, eks_registry: Registry) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", files={"acme_ops.py": ACME_OPS})
    load_plugins(tree, host=host)
    first = eks_registry.op("acme.ping")
    assert load_plugins(tree, host=host) == ["acme:acme_ops"]
    assert eks_registry.op("acme.ping") is first


def test_loading_after_a_registry_restore_registers_again(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", files={"acme_ops.py": ACME_OPS})
    state = eks_registry.snapshot()
    load_plugins(tree, host=host)
    eks_registry.restore(state)
    assert not eks_registry.has_op("acme.ping")
    load_plugins(tree, host=host)
    assert eks_registry.op("acme.ping").provenance == Provenance("acme", "1.2.0")


def test_reserved_plugin_ids_are_rejected(tmp_path: Path) -> None:
    for reserved in ("eks", "project"):
        with pytest.raises(ManifestError, match=f"plugin.id '{reserved}' is reserved"):
            parse_manifest({"plugin": {"id": reserved}}, tmp_path)
    assert parse_manifest({"plugin": {"id": "eks.drivers"}}, tmp_path).id == "eks.drivers"
    assert parse_manifest({"plugin": {"id": "project-tools"}}, tmp_path).id == "project-tools"


PACKAGE_INIT = """
    from acme_pkg import impl

    VERSION = impl.VERSION
"""

PACKAGE_IMPL = """
    import eks

    VERSION = 1

    @eks.op("acme")
    class Ping(eks.Args):
        pass
"""


def test_submodule_registrations_come_back_after_a_restore(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    # The manifest lists the package; the op lives in a submodule it imports.
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", modules='["acme_pkg"]',
                 files={"acme_pkg/__init__.py": PACKAGE_INIT, "acme_pkg/impl.py": PACKAGE_IMPL})
    state = eks_registry.snapshot()
    assert load_plugins(tree, host=host) == ["acme:acme_pkg"]
    first = eks_registry.op("acme.ping")
    assert first.args.__module__.endswith(".acme_pkg.impl")
    eks_registry.restore(state)
    assert not eks_registry.has_op("acme.ping")
    load_plugins(tree, host=host)
    again = eks_registry.op("acme.ping")
    assert again.provenance == Provenance("acme", "1.2.0")
    assert again is not first
    # Once restored, a further load is a no-op.
    load_plugins(tree, host=host)
    assert eks_registry.op("acme.ping") is again


def test_a_changed_plugin_may_rename_and_drop_registrations(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    root = tree / ".harness" / "plugins" / "acme"
    write_plugin(root, "acme", files={"acme_ops.py": """
        import eks

        @eks.op("acme", name="thing")
        class Foo(eks.Args):
            pass

        @eks.op("acme")
        class Gone(eks.Args):
            pass
    """})
    load_plugins(tree, host=host)
    foo = eks_registry.op("acme.thing").args
    assert foo.__name__ == "Foo"
    write_plugin(root, "acme", version="1.3.0", files={"acme_ops.py": """
        import eks

        @eks.op("acme", name="thing")
        class Bar(eks.Args):
            pass
    """})
    fresh = PluginHost(host.paths, host.config, builtin_root=tmp_path / "no-builtins", include_packages=False)
    assert load_plugins(tree, host=fresh) == ["acme:acme_ops"]
    thing = eks_registry.op("acme.thing")
    assert thing.args.__name__ == "Bar"
    # The changed plugin was imported under a new scope generation.
    assert thing.args.__module__ != foo.__module__
    assert thing.args.__module__.endswith(".acme_ops")
    assert thing.provenance == Provenance("acme", "1.3.0")
    assert not eks_registry.has_op("acme.gone")


def test_a_failing_plugin_module_leaves_no_partial_registrations(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", files={"acme_ops.py": """
        import eks

        @eks.op("acme")
        class Half(eks.Args):
            pass

        raise RuntimeError("boom")
    """})
    with pytest.raises(DiscoveryError, match=r"plugin 'acme'.*'acme_ops'.*RuntimeError: boom"):
        load_plugins(tree, host=host)
    assert not eks_registry.has_op("acme.half")
    with pytest.raises(DiscoveryError, match="boom"):
        load_plugins(tree, host=host)


def test_a_failing_module_rolls_back_the_whole_plugin(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    # acme_a loads fine, acme_b fails: nothing of the plugin may stay registered.
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    root = tree / ".harness" / "plugins" / "acme"
    write_plugin(root, "acme", modules='["acme_a", "acme_b"]', files={
        "acme_a.py": """
            import eks

            @eks.op("acme")
            class Alpha(eks.Args):
                pass
        """,
        "acme_b.py": """
            import eks

            @eks.op("acme")
            class Beta(eks.Args):
                pass

            raise RuntimeError("beta broke")
        """,
    })
    with pytest.raises(DiscoveryError, match=r"plugin 'acme'.*'acme_b'.*RuntimeError: beta broke"):
        load_plugins(tree, host=host)
    assert not eks_registry.has_op("acme.alpha")
    assert not eks_registry.has_op("acme.beta")
    # Once fixed, both modules load under a fresh host.
    write_plugin(root, "acme", version="1.2.1", modules='["acme_a", "acme_b"]', files={"acme_b.py": """
        import eks

        @eks.op("acme")
        class Beta(eks.Args):
            pass
    """})
    fresh = PluginHost(host.paths, host.config, builtin_root=tmp_path / "no-builtins", include_packages=False)
    assert load_plugins(tree, host=fresh) == ["acme:acme_a", "acme:acme_b"]
    assert eks_registry.op("acme.alpha").provenance == Provenance("acme", "1.2.1")
    assert eks_registry.op("acme.beta").provenance == Provenance("acme", "1.2.1")


def test_a_failing_reload_restores_the_previous_plugin_state(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    # A plugin that loaded once keeps its earlier registrations when a later reload fails part way.
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    root = tree / ".harness" / "plugins" / "acme"
    write_plugin(root, "acme", modules='["acme_a", "acme_b"]', files={
        "acme_a.py": """
            import eks

            @eks.op("acme")
            class Alpha(eks.Args):
                pass
        """,
        "acme_b.py": """
            import eks

            @eks.op("acme")
            class Beta(eks.Args):
                pass
        """,
    })
    load_plugins(tree, host=host)
    alpha = eks_registry.op("acme.alpha")
    beta = eks_registry.op("acme.beta")
    # Losing beta forces acme_b to re-execute on the next load; it now fails.
    eks_registry.drop_module(beta.args.__module__, "acme")
    (root / "acme_b.py").write_text("raise RuntimeError('beta broke')\n", encoding="utf-8")
    with pytest.raises(DiscoveryError, match="beta broke"):
        load_plugins(tree, host=host)
    assert eks_registry.op("acme.alpha") is alpha
    assert not eks_registry.has_op("acme.beta")
    # Once repaired, the plugin loads whole again (under a new scope generation).
    (root / "acme_b.py").write_text(
        "import eks\n\n\n@eks.op('acme')\nclass Beta(eks.Args):\n    pass\n", encoding="utf-8")
    assert load_plugins(tree, host=host) == ["acme:acme_a", "acme:acme_b"]
    assert eks_registry.op("acme.alpha").provenance == Provenance("acme", "1.2.0")
    assert eks_registry.op("acme.beta") is not beta


def test_package_layout_modules(tmp_path: Path, host: PluginHost, eks_registry: Registry) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme.tools"])
    write_plugin(tree / ".harness" / "plugins" / "tools", "acme.tools", version="0.3.0",
                 modules='["acme_tools.ops", "acme_tools.presets"]',
                 files={"src/acme_tools/__init__.py": "",
                        "src/acme_tools/ops.py": """
                            import eks

                            @eks.op("acme_tools")
                            class Sync(eks.Args):
                                pass
                        """,
                        "src/acme_tools/presets.py": """
                            import eks
                            from eks_harness.ir.presets import Viewport

                            @eks.preset("acme-kiosk")
                            def kiosk():
                                return Viewport(width=1080, height=1920)
                        """})
    assert load_plugins(tree, host=host) == ["acme.tools:acme_tools.ops", "acme.tools:acme_tools.presets"]
    assert eks_registry.op("acme_tools.sync").provenance == Provenance("acme.tools", "0.3.0")
    assert eks_registry.preset("viewport", "acme-kiosk").provenance == Provenance("acme.tools", "0.3.0")


def test_untrusted_and_disabled_plugins_are_skipped(
    tmp_path: Path, host: PluginHost, eks_registry: Registry
) -> None:
    tree = write_tree(tmp_path / "shop", trust=["gamma"], disable=["gamma"])
    write_plugin(tree / ".harness" / "plugins" / "beta", "beta", files={"acme_ops.py": "raise SystemExit(3)\n"})
    write_plugin(tree / ".harness" / "plugins" / "gamma", "gamma", files={"acme_ops.py": "raise SystemExit(4)\n"})
    states = {record.id: record.state for record in host.records(tree)}
    assert states == {"beta": "pending", "gamma": "disabled"}
    assert load_plugins(tree, host=host) == []
    assert not eks_registry.has_op("acme.ping")


def test_plugins_without_modules_are_skipped(tmp_path: Path, host: PluginHost) -> None:
    tree = write_tree(tmp_path / "shop", trust=["plain"])
    write_plugin(tree / ".harness" / "plugins" / "plain", "plain", modules="[]")
    assert [record.state for record in host.records(tree)] == ["active"]
    assert load_plugins(tree, host=host) == []


def test_missing_module_raises_discovery_error(tmp_path: Path, host: PluginHost) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", modules='["acme_missing"]')
    with pytest.raises(DiscoveryError, match=r"plugin 'acme'.*cannot import module 'acme_missing'"):
        load_plugins(tree, host=host)


def test_registry_errors_in_a_plugin_module_name_the_plugin(tmp_path: Path, host: PluginHost) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", files={"acme_ops.py": """
        import eks

        @eks.op("ui")
        class AcmeTap(eks.Args):
            pass
    """})
    with pytest.raises(DiscoveryError, match=r"plugin 'acme'.*'acme_ops'.*RegistryError.*built-in family 'ui'"):
        load_plugins(tree, host=host)


def test_default_host_uses_the_isolated_harness_home(tmp_path: Path, eks_registry: Registry) -> None:
    tree = write_tree(tmp_path / "shop", trust=["acme"])
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme", files={"acme_ops.py": ACME_OPS})
    assert "acme:acme_ops" in load_plugins(tree)
    assert eks_registry.op("acme.ping").provenance.plugin == "acme"


def test_no_tree_loads_only_global_plugins(host: PluginHost) -> None:
    assert load_plugins(None, host=host) == []


def test_manifest_modules_parsing(tmp_path: Path) -> None:
    root = write_plugin(tmp_path / "p", "acme", modules='["acme.ops", "acme_presets", "_private.mod"]')
    manifest = load_manifest(root)
    assert manifest.modules == ("acme.ops", "acme_presets", "_private.mod")
    assert manifest.summary()["modules"] == ["acme.ops", "acme_presets", "_private.mod"]
    bare = parse_manifest({"plugin": {"id": "bare"}}, tmp_path)
    assert bare.modules == ()
    assert bare.summary()["modules"] == []


@pytest.mark.parametrize(
    ("modules", "match"),
    [('"acme.ops"', "must be a list"), ('[1]', "not a dotted module name"), ('["acme ops"]', "not a dotted"),
     ('["acme..ops"]', "not a dotted"), ('["acme.ops."]', "not a dotted"), ('["1acme"]', "not a dotted"),
     ('["acme:ops"]', "not a dotted"), ('["a", "a"]', "twice"), ('{ x = 1 }', "must be a list")],
)
def test_manifest_modules_errors(tmp_path: Path, modules: str, match: str) -> None:
    root = write_plugin(tmp_path / "p", "acme", modules=modules)
    with pytest.raises(ManifestError, match=match):
        load_manifest(root)
