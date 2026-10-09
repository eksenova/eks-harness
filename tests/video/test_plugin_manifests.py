from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

from eks_harness.config import Config
from eks_harness.paths import resolve_paths
from eks_harness.plugins.discovery import BUILTIN_ROOT, plugin_dirs
from eks_harness.plugins.host import PluginHost
from eks_harness.plugins.manifest import load_manifest
from eks_harness.video.plugins import registry

MANIFESTS = [load_manifest(root) for root in plugin_dirs(BUILTIN_ROOT)]
ENTRIES = [(m.id, c) for m in MANIFESTS for c in m.contributions if c.entry]


def test_builtin_video_manifests_exist() -> None:
    ids = {m.id for m in MANIFESTS}
    assert {"eks.video", "eks.blender"} <= ids
    blender = next(m for m in MANIFESTS if m.id == "eks.blender")
    assert blender.enabled_by_default is False
    video = next(m for m in MANIFESTS if m.id == "eks.video")
    kinds = {c.type for c in video.contributions}
    assert {"effect", "encoder", "marker_source", "captioner", "media_provider"} <= kinds


@pytest.mark.parametrize(("plugin_id", "contribution"), ENTRIES, ids=[f"{p}:{c.type}:{c.id}" for p, c in ENTRIES])
def test_every_builtin_entry_imports(plugin_id: str, contribution) -> None:
    module_name, _, attr = contribution.entry.partition(":")
    target = importlib.import_module(module_name)
    for part in attr.split("."):
        target = getattr(target, part)
    assert target is not None


def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_EFFECTS", "_CAPTIONERS", "_EASINGS", "_MEDIA_PROVIDERS", "_ENCODERS", "_MARKER_EXTRACTORS",
                 "_MEDIA_RENDERERS", "_TRANSITIONS"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_LOADED", False)


def test_load_from_host_respects_blender_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fresh_registry(monkeypatch)
    paths = resolve_paths().ensure()
    host = PluginHost(paths, Config(paths), include_packages=False)
    loaded = registry.load_from_host(host)
    assert "eks.video:blur" in loaded["effect"]
    assert "eks.blender:blender" not in loaded["media_renderer"]
    assert registry.get_media_renderer("blender_scene") is None
    tree = tmp_path / "repo"
    (tree / ".harness").mkdir(parents=True)
    (tree / ".harness" / "project.toml").write_text('[plugins]\nenable = ["eks.blender"]\n', encoding="utf-8")
    _fresh_registry(monkeypatch)
    loaded = registry.load_from_host(PluginHost(paths, Config(paths), include_packages=False), tree)
    assert "eks.blender:blender" in loaded["media_renderer"]
    assert registry.get_media_renderer("blender_scene") is not None
    assert registry.registry_loaded()


def test_repo_transition_plugin_renders_through_filter_graph(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    _fresh_registry(monkeypatch)
    tree = tmp_path / "repo"
    plugin = tree / ".harness" / "plugins" / "fx"
    plugin.mkdir(parents=True)
    (plugin / "harness-plugin.toml").write_text(textwrap.dedent("""
        [plugin]
        id = "acme.fx"

        [[contributes.transition]]
        id = "swirl"
        entry = "acme_fx_transitions:Swirl"
    """), encoding="utf-8")
    (plugin / "acme_fx_transitions.py").write_text(textwrap.dedent("""
        class Swirl:
            name = "swirl"

            def filter_complex(self, params, duration, offset, boundary_beats):
                return f"[a][b]xfade=transition=radial:duration={duration:.6f}:offset={offset:.6f}[v]"
    """), encoding="utf-8")
    (tree / ".harness" / "project.toml").write_text('[plugins]\ntrust = ["fx"]\n', encoding="utf-8")
    paths = resolve_paths().ensure()
    loaded = registry.load_from_host(PluginHost(paths, Config(paths), include_packages=False), tree)
    assert loaded["transition"] == ["acme.fx:swirl"]
    from eks_harness.video.ir.transitions import PluginTransition
    from eks_harness.video.render.transitions import _build_filter_complex, _is_xfade

    transition = PluginTransition(name="swirl", duration=0.4)
    assert _is_xfade(transition)
    graph = _build_filter_complex(transition, 0.4, 1.0)
    assert "xfade=transition=radial:duration=0.400000:offset=1.000000[v]" in graph
    assert not _is_xfade(PluginTransition(name="missing"))
